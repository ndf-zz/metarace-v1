
"""UCI Hour Record handler for trackmeet."""

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
from metarace import eventdb
from metarace import jsonconfig
from metarace import riderdb
from metarace import strops
from metarace import timerpane
from metarace import report

# config version string
EVENT_ID = 'hourrec'

# scb function key mappings (also trig announce)
key_reannounce = 'F4'                # (+CTRL) calls into delayed announce
key_timerwin = 'F6'                 # re-show timing window

# timing function key mappings
key_armstart = 'F5'                  # arm start -> countdown
key_armfinish = 'F9'                 # override finish trigger

# extended function key mappings
key_reset = 'F5'                     # + ctrl for clear/abort
key_falsestart = 'F6'		     # + ctrl for false start
key_abort = 'F7'		     # + ctrl abort A

class hourrec(object):
    """Data handling for Hour record."""

    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or 'None'
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
                    self.abortrider()
                    return True
            elif key[0] == 'F':
                if key == key_armstart:
                    self.armstart()
                    return True
                elif key == key_armfinish:
                    self.armfinish()
                    return True
                elif key == key_timerwin:
                    self.showtimerwin()
                    glib.idle_add(self.delayed_announce)
                    return True
        return False

    def recalc(self):
        """Recalc runtime from current state."""
        if self.finish is None and self.elapsed == u'' and self.start is not None and len(self.splitlist) > 0:
            self.elapsed = (self.splitlist[-1] - self.start).rawtime(0)
        if self.finish is not None:
            # self.d = 0              # dist in m     (trunc to m)
            self.TC = self.lapcount - 1
            self.TTC = None
            if len(self.splitlist) > 1:
                lasttime = self.finish	# same as self.splitlist[-1]
                belltime = self.splitlist[-2]
                # time of the last complete lap
                self.TTC = lasttime - belltime
                # time remaining to ride at the beginning of the last lap
                self.TRC = self.reclen - (belltime - self.start)
                # additional distance
                self.DiC = int(float(self.TRC.timeval)/float(self.TTC.timeval) * float(self.LPi))
                # distance covered in the hour
                self.D = self.DiC + self.TC * self.LPi
                self.compute = u'Compute: D={0}m, LPi={1}m, TC={2}, DiC={3}, TTC={4}, TRC={5}'.format(self.D, self.LPi, self.TC, self.DiC, self.TTC.rawtime(3), self.TRC.rawtime(3))
                self.log.info(self.compute)
            self.projection = None
        else:
            # update projection
            if len(self.splitlist) > 4:
                et = self.splitlist[-1] - self.start
                rt = self.reclen - et
                di = self.LPi * self.lapcount                
                pi = self.splitlist[-5]
                pd = self.splitlist[-1]
                pr = 1000.0/(float(pd.timeval) - float(pi.timeval))
                de = di + int(float(rt.timeval) * pr)
                self.log.info(u'Projected final dist: ' + repr(de))
                if de < self.maxproj and de > self.minproj:
                    self.projection = de
                else:
                    self.projection = None

    def loadconfig(self):
        """Load race config from disk."""
        cr = jsonconfig.config({u'event':{u'riderstr':u'',
                                          u'rideruci':u'',
                                          u'recordname':u'Hour Record',
                                          u'wallstart': None,
                                          u'start': None,
                                          u'finish': None,
                                          u'lstart': None,
                                          u'target': 0,
                                          u'reclen': '1h00:00',
                                          u'minlap': '14.0',
                                          u'record': None,
                                          u'projlap': 12, 
                                          u'lpi': 250, 
                                          u'minproj': 30000,
                                          u'maxproj': 60000,
                                          u'lapcount': 0,
                                          u'splitlist': []}})
                                          
        cr.add_section(u'event')
        # check for config file
        try:
            with open(self.configpath, 'rb') as f:
                cr.read(f)
        except Exception as e:
            self.log.error(u'Reading event config: ' + unicode(e))

        self.riderstr = cr.get(u'event',u'riderstr')
        self.rideruci = cr.get(u'event',u'rideruci')
        self.recordname = cr.get(u'event',u'recordname')

        self.reclen = tod.mktod(cr.get(u'event', u'reclen'))
        if self.reclen is None:
            self.log.error(u'Invalid record length: Reset to 1h00:00')
            self.reclen = tod.tod(u'1h00:00')
        self.minlap = tod.mktod(cr.get(u'event', u'minlap'))
        if self.minlap is None:
            self.log.error(u'Invalid min lap time: Reset to 14.0s')
            self.minlap = tod.tod(u'14.0')
        self.start = tod.mktod(cr.get(u'event', u'start'))
        self.finish = tod.mktod(cr.get(u'event', u'finish'))
        self.lstart = tod.mktod(cr.get(u'event', u'lstart'))
        self.wallstart = tod.mktod(cr.get(u'event', u'wallstart'))

        self.target = strops.confopt_posint(cr.get(u'event', u'target'))
        self.record = strops.confopt_posint(cr.get(u'event', u'record'))
        self.LPi = strops.confopt_posint(cr.get(u'event', u'lpi'),250)
        self.projlap = strops.confopt_posint(cr.get(u'event', u'projlap'),12)
        self.minproj = strops.confopt_posint(cr.get(u'event', u'minproj'),30000)
        self.maxproj = strops.confopt_posint(cr.get(u'event', u'maxproj'),60000)
        self.lapcount = strops.confopt_posint(cr.get(u'event', u'lapcount'))
        self.splitlist = []
        for lt in cr.get(u'event', u'splitlist'):
            nlt = tod.mktod(lt)
            if nlt is not None:
                self.splitlist.append(nlt)
        # check count of laps agains splits
        if len(self.splitlist) != self.lapcount:
            self.log.error(u'SPLIT LIST != LAPCOUNT')

        # arm the front straight
        if self.winopen:
            self.meet.timer.arm(timy.CHAN_PA)
            self.meet.timer.armlock(True)
            self.meet.scbwin = scbwin.scbtt(scb=self.meet.scb,
                                            header=self.riderstr,
                                            subheader=self.recordname.upper())
            self.meet.scbwin.reset()
        # recalc
        self.recalc()

    def saveconfig(self):
        """Save race to disk."""
        if self.readonly:
            self.log.error('Attempt to save readonly ob.')
            return
        self.log.info(u'saveconfig')

        cw = jsonconfig.config()
        cw.add_section(u'event')

        cw.set(u'event', u'riderstr', self.riderstr)
        cw.set(u'event', u'rideruci', self.rideruci)
        cw.set(u'event', u'recordname', self.recordname)
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
         
        slout = []
        for lt in self.splitlist:
            slout.append(lt.rawtime())
        cw.set(u'event', u'splitlist', slout)

        self.log.debug(u'Saving config to: ' + repr(self.configpath))
        with open(self.configpath, 'wb') as f:
            cw.write(f)

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
        cnt = 0
        sec = report.bullet_text()
        sec.heading = u' '.join([self.event[u'pref'],
                                 self.event[u'info']]).strip()
        substr = u' '.join([self.event[u'dist'],
                            self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr
        if self.event[u'reco']:
            sec.footer = self.event[u'reco'] 
        # the rider
        sec.lines.append([u'',self.riderstr + u' ' + self.rideruci])
        if self.record is not None:
            tstr = u'{0:0.3f} km'.format(self.record/1000.0)
            sec.lines.append([u'', u'Record: ' + tstr])
        if self.target is not None:
            tstr = u'{0:0.3f} km'.format(self.target/1000.0)
            sec.lines.append([u'', u'Target: ' + tstr])
        if self.wallstart is not None:
            sec.lines.append([u'', u'Start Time: ' + self.wallstart.meridian()])
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
            self.log.info(u'delayed_announce')

    def shutdown(self, win=None, msg=u'Exiting'):
        """Terminate race object."""
        if self.winopen:
            self.meet.timer.armlock(False)
        if not self.readonly:
            self.saveconfig()
        self.log.debug(u'Race Shutdown: ' + msg)
        self.winopen = False

    def do_properties(self):
        """Run race properties dialog."""
        self.log.debug(u'Race Properties')

    def result_gen(self):
        """Generator function to export a final result."""
        yield [u'', u'', u'', u'']

    def result_report(self, recurse=False):
        """Return a list of report sections containing the race result."""
        ret = []
        sec = report.bullet_text()
        sec.heading = u' '.join([self.event[u'pref'], self.event[u'info']]).strip()
        substr = u' '.join([self.event[u'dist'],
                             self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr
        if self.event[u'reco']:
            sec.footer = self.event[u'reco']
        #if self.wallstart is not None:
            #sec.subheading = u'Start: ' + self.wallstart.meridian()

        sec.lines.append([u'',self.riderstr + u' ' + self.rideruci])
        # Distance measure
        if self.finish is not None:
            if self.D is not None and self.D > 0:
                dstr = u'{0:0.3f} km'.format(self.D/1000.0)
                sec.lines.append([u'', u'Final distance: ' + dstr])
                if self.record is not None:
                    if self.D > self.record:
                        sec.lines.append(['',u'New record by {} metre{}'.format(self.D-self.record,strops.plural(self.D-self.record))])
                    else:
                        tstr = u'{0:0.3f} km'.format(self.record/1000.0)
                        sec.lines.append([u'', u'{} metre{} short of existing record: '.format(self.record-self.D, strops.plural(self.record-self.D)) + tstr])
            sec.lines.append([u'', u'Complete laps: ' + unicode(self.lapcount-1)])
            if self.compute:
                sec.lines.append([u'', u'Additional distance: {} m'.format(self.DiC)])
                sec.lines.append([u'', self.compute])
        else:
            if self.record is not None:
                tstr = u'{0:0.3f} km'.format(self.record/1000.0)
                sec.lines.append([u'', u'Record: ' + tstr])
            if self.target is not None:
                tstr = u'{0:0.3f} km'.format(self.target/1000.0)
                sec.lines.append([u'', u'Target: ' + tstr])
            if self.wallstart is not None:
                sec.lines.append([u'', u'Start Time: ' + self.wallstart.meridian()])
            if self.projection is not None:
                tstr = u'{0:0.3f} km'.format(self.projection/1000.0)
                sec.lines.append([u'', u'Projection: ' + tstr])
            sec.lines.append([u'', u'Elapsed: ' + self.elapsed])
            sec.lines.append([u'', u'Laps: ' + unicode(self.lapcount)])
        ret.append(sec)
        if self.start is not None:
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
                nd = int(0.010 + self.LPi*count/1000.0)
                if nd != ld:
                    lstr += '  / {} km'.format(nd)
                    ld = nd
                sec.lines.append([u'', u'', lstr, u'', laptime.rawtime(3), split.rawtime(3)])
                lt = st
                count += 1
            ret.append(sec)
        return ret

    def addrider(self, bib='', info=None):
        return None

    def editent_cb(self, entry, col):
        """Shared event entry update callback."""
        if col == u'pref':
            self.event[u'pref'] = entry.get_text().decode('utf-8', 'replace')
        elif col == u'info':
            self.event[u'info'] = entry.get_text().decode('utf-8', 'replace')
        self.update_expander_lbl_cb()

    def update_expander_lbl_cb(self):
        """Update race info expander label."""
        self.info_expand.set_label('Race Info : '
                    + self.meet.racenamecat(self.event, 64))

    def split_trig(self, t):
        """Register lap trigger."""
        if self.start is not None:
            lastlap = None
            if len(self.splitlist) > 0:
                lastlap = self.splitlist[-1]
            else:
                lastlap = self.start
            if t > lastlap:
                laptime = t - lastlap
                if laptime > self.minlap:
                    elap = t - self.start
                    if elap <= self.reclen:
                        self.lapcount += 1
                        self.splitlist.append(t)
                        self.recalc()
                        self.curlap = laptime
                        glib.idle_add(self.scblap)
                        self.log.info(u'Lap ' + repr(self.lapcount) + ': ' + laptime.rawtime(2) + u' @ ' + t.rawtime(2))
                        if laptime < tod.tod(u'60'):
                            self.lastlapstr = laptime.rawtime(2)
                        else:
                            self.lastlapstr = laptime.rawtime(0)
                    else:
                        if self.finish is None:
                            self.lapcount += 1
                            self.splitlist.append(t)
                            self.finish = t
                            self.curlap = laptime
                            self.recalc()
                            self.log.info(u'Final Lap Completed.')
                        else:
                            self.log.info(u'Duplicate finish pass.')
                    # and ask meet for an export
                    self.meet.delayed_export()
                else:
                    self.log.info(u'Ignored short lap.')
            else:
                self.log.info(u'Invalid trigger.')
        else:
            self.log.info(u'Ignored trig without start.')


    def scblap(self):
        # output to main scoreboard
        self.meet.scbwin.setline1(
          strops.truncpad(u'Elapsed: ',self.meet.scb.linelen-12, align='r')+
          strops.truncpad(self.elapsed,12)
        )
        
        if self.lapcount > 0:
            ## display kilometre updates elsewhere?
            if self.lapcount > 3:
                pass
                #kilo = self.lapcount // 4
                #self.meet.scbwin.setr1(u'Lap {0}, {1}km:'.format(
                    #self.lapcount,kilo))
            #else:
            self.meet.scbwin.setr1(u'Lap {0}:'.format(self.lapcount))
            self.meet.scbwin.sett1(self.lastlapstr)
        else:
            self.meet.scbwin.setline2(u'')
            self.meet.scbwin.setr1(u'')
            self.meet.scbwin.sett1(u'')
        if self.record:
            self.meet.scbwin.setline2(
             strops.truncpad(u'Record: ',self.meet.scb.linelen-12, align='r')+
             strops.truncpad(u'{0:0.3f}km'.format(self.record/1000.0),12)
            )
        elif self.target:
            self.meet.scbwin.setline2(
             strops.truncpad(u'Target: ',self.meet.scb.linelen-12, align='r')+
             strops.truncpad(u'{0:0.3f}km'.format(self.target/1000.0),12)
            )
        if self.lapcount > self.projlap and self.projection is not None:
            self.meet.scbwin.setr2(u'Projection:')
            self.meet.scbwin.sett2(u'{0:0.1f}  km'.format(self.projection/1000.0))
        else:
            self.meet.scbwin.setr2(u'')
            self.meet.scbwin.sett2(u'')
        self.meet.scbwin.update()

        # telegraph outputs
        self.meet.announce.publish_cmd(u'projection',unicode(self.projection))
        self.meet.announce.publish_cmd(u'lapcount',unicode(self.lapcount))
        self.meet.announce.publish_cmd(u'laptime',self.lastlapstr)
        self.meet.announce.publish_cmd(u'elapsed',self.elapsed)

        # on the gemini - use B/T dual timer mode
        #self.meet.gemini.set_bib(str(self.lapcount),0)
        #self.meet.gemini.set_time(self.lastlapstr,0)
        #self.meet.gemini.set_time(self.elapsed,1)
        #self.meet.gemini.show_dual()

        return False

    def rftimercb(self, e):
        """Handle rftimer event."""
        if e.refid == '':       # got a trigger
            #return self.starttrig(e)
            return False
        return False

    def timercb(self, e):
        """Handle a timer event."""
        chan = timy.chan2id(e.chan)
        if chan == timy.CHAN_START:
            if self.start is None:
                self.lstart = tod.now()
                self.start = e
                self.log.info(u'Set Start: ' + e.rawtime())
            else:
                self.log.info(u'Spurious start trig: ' + e.rawtime())
        elif chan == timy.CHAN_PA:
            self.split_trig(e)
        else:
            self.log.info(u'Trigger: ' + repr(chan) + u' : ' + e.rawtime())
        return False

    def timeout(self):
        """Update scoreboard and respond to timing events."""
        if not self.winopen:
            return False
        now = tod.now()
        nelap = self.elapsed
        dofinishtxt = False
        dostarttxt = False
        if self.lstart is not None and self.finish is None:
             tot = now - self.lstart
             #self.elaptod = tot
             if tot >= (self.reclen + tod.tod(u'5:00')):
                 nelap = u'--:--'
             else:
                 nelap = tot.rawtime(0)
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
            if dofinishtxt:
                self.meet.scbwin.setline1(u'')
                self.meet.scbwin.setr1(u'Result:')
                #self.meet.scbwin.setr1(u'Final Distance:')
                if self.D is not None and self.D > 0: 
                    self.meet.scbwin.sett1(u'{0:0.3f}km'.format(self.D/1000.0))
                if self.record is not None:
                    if self.D > self.record:
                        self.meet.scbwin.setline2(u'NEW RECORD'.center(self.meet.scb.linelen))
                    else:
                        self.meet.scbwin.setline2(u'')
                else:
                    self.meet.scbwin.setline2(u'')
                self.meet.scbwin.setr2(u'')
                self.meet.scbwin.sett2(u'')
            elif dostarttxt:
                if self.record:
                    self.meet.scbwin.setr1(u'Record:')
                    self.meet.scbwin.sett1(u'{0:0.3f}km'.format(self.record/1000.0))
                elif self.target:
                    self.meet.scbwin.setr1(u'Target:')
                    self.meet.scbwin.sett1(u'{0:0.3f}km'.format(self.target/1000.0))
                if self.wallstart is not None:
                    line1 = strops.truncpad(u'Start Time: ',self.meet.scb.linelen-12,align='r')+strops.truncpad(self.wallstart.meridian(),12,align='l')
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
            
        ## updates and running lap
	# hl board, 
        return True

    def armstart(self):
        """Arm timer for start trigger."""
        if self.start is None:
            self.log.info(u'Arm Start.')
            self.meet.timer.arm(timy.CHAN_START)
        else:
            self.log.info(u'Event already started.')

    def armsplit(self, sp, cid=timy.CHAN_200):
        """Arm timer for a 50m/200m split."""
        self.DiC = 0            # additional distance
        self.log.info(u'armsplit')

    def abortrider(self):
        """Abort the attempt."""
        self.log.info(u'abortrider')

    def falsestart(self):
        """Register false start."""
        self.log.info(u'Falsestart')

    def armfinish(self):
        """Arm timer for finish trigger."""
        self.log.info(u'armfinish')
        
    def showtimerwin(self):
        """Show timer window on scoreboard."""
        self.log.info(u'showtimerwin')
        self.meet.scbwin = scbwin.scbtt(scb=self.meet.scb,
                                        header=self.riderstr,
                                        subheader=self.recordname.upper())
        self.meet.scbwin.reset()
        self.recalc()

    def toidle(self):
        """Set timer to idle state."""
        self.finish = None
        self.start = None
        self.lstart = None
        self.lapcount = 0       # current lap counter
        self.elapsed = u''      # current elapsed time str
        #self.elaptod = tod.tod(u'0')
        self.lastlapstr = u'     '      # last lap as string
        self.splitlist = []

        # set lapcount
        self.log.info(u'Reset event state to idle')

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
        self.readonly = not ui
        self.winopen = ui
        self.meet = meet
        self.event = event      # Note: now a treerowref
        self.evno = event[u'evid']
        self.evtype = event[u'type']
        self.series = event[u'seri']
        self.configpath = meet.event_configfile(self.evno)
        self.autospec = ''

        self.log = logging.getLogger('hourrec')
        self.log.setLevel(logging.DEBUG)
        self.log.debug('Creating new hour rec event: ' + str(self.evno))

        # model
        self.reclen = tod.tod(u'1h00:00')	# record duration
        self.minlap = tod.tod(u'14.0')	# min lap time
        self.compute =u''
        self.riderstr = u''	# rider name string
	self.rideruci = u''	# rider info string -> uci code
        self.wallstart = None	# advertised start time
        self.finish = None	# timer finish tod
        self.start = None	# timer start tod
        self.lstart = None	# local start tod
        self.target = 0		# current target in m
        self.record = None	# current record in m
        self.projlap = 12	# start showing projection after this many laps
        self.minproj = 30000	# minimum possible projection
        self.maxproj = 60000	# maximum possible projection
        self.projection = None	# current projection in m
        self.avglap = None	# current lap avg
        self.lapcount = 0	# current lap counter
        self.elapsed = u''	# current elapsed time str
        #self.elaptod = tod.tod(u'0')
        self.onestart = True	#
        self.lastlapstr = u'     '	# last lap as string
        self.splitlist = []
                
        # computes
        self.D = 0		# dist in m	(trunc to m)
	self.LPi = 250		# len of track	(should be int)
	self.TC = 0		# number of complete laps before last lap
	self.DiC = 0		# additional distance
	self.TTC = None		# time of last complete lap
	self.TRC = None		# time remaining to ride at beginning of
				# last lap

        # UI
        self.frame = gtk.VBox(False, 5)
        self.frame.connect('destroy', self.shutdown)
        if ui:
            # interactive elements
            pass
