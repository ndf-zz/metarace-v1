"""Sprint round handler for trackmeet."""

# Note: A bug in the model/interface causes incorrect results to be
#       displayed on scb if a result is changed. Work required to fix.

import gtk
import glib
import gobject
import pango
import logging
import os

import metarace
from metarace import tod
from metarace import jsonconfig
from metarace import timy
from metarace import unt4
from metarace import riderdb
from metarace import scbwin
from metarace import uiutil
from metarace import strops
from metarace import report

# config version string
EVENT_ID = 'tracksprnd-1.0'

# race gobject model column constants
COL_CONTEST = 0  # contest ID '1v16'
COL_A_NO = 1  # Number of A rider
COL_A_STR = 2  # Namestr of A rider
COL_A_PLACE = 3  # Place string of A rider
COL_B_NO = 4  # Number of B rider
COL_B_STR = 5  # Namestr of B rider
COL_B_PLACE = 6  # Place string of B rider
COL_200M = 7  # time for last 200m
COL_WINNER = 8  # no of 'winner'
COL_COMMENT = 9  # decisions of commissaire's panel

# scb function key mappings
key_startlist = 'F3'  # show starters in table
key_results = 'F4'  # recalc/show result window

# timing function key mappings
key_armstart = 'F5'  # arm for start/200m impulse
key_showtimer = 'F6'  # show timer
key_armfinish = 'F9'  # arm for finish impulse
key_win_a = 'F11'  # A rider wins
key_win_b = 'F12'  # B rider wins

# extended function key mappings
key_abort = 'F5'  # + ctrl for clear/abort
key_walk_a = 'F9'  # + ctrl for walk over
key_walk_b = 'F10'
key_rel_a = 'F11'  # + ctrl for relegation
key_rel_b = 'F12'


class sprnd(object):
    """Data handling for sprint rounds."""

    def addrider(self, bib='', info=None):
        """Add specified rider to race model."""
        rstr = u''
        rcom = u''
        cstack = []
        for cr in self.contests:
            # check for 'first' empty slot in A riders
            if cr[COL_A_NO] == u'':
                cr[COL_A_NO] = bib
                cr[COL_A_STR] = self.rider_name(bib)
                cr[COL_A_PLACE] = u''  # LOAD?
                ## special case the bye here
                if cr[COL_CONTEST] == u'bye':
                    cr[COL_A_PLACE] = u' '
                    cr[COL_B_STR] = u' '
                    cr[COL_B_PLACE] = u' '
                    cr[COL_B_NO] = u' '
                    cr[COL_WINNER] = bib  # auto win the bye rider
                return
            cstack.insert(0, cr)  # do these get re-used?
        for cr in cstack:
            # reverse contests for B riders
            if cr[COL_B_NO] == u'':
                cr[COL_B_NO] = bib
                cr[COL_B_STR] = self.rider_name(bib)
                cr[COL_B_PLACE] = u''  # LOAD?
                return
        self.log.warn('Not enough heats for the specified starters: ' +
                      repr(bib))

    def delrider(self, bib):
        """Remove specified rider from the model."""
        for c in self.contests:
            if c[COL_A_NO] == bib:
                c[COL_A_NO] = u''
                c[COL_A_PLACE] = u''
                c[COL_A_STR] = u''
            elif c[COL_B_NO] == bib:
                c[COL_B_NO] = u''
                c[COL_B_PLACE] = u''
                c[COL_B_STR] = u''
            if c[COL_WINNER] == bib:
                c[COL_200M] = None
                c[COL_WINNER] = u''

    def loadconfig(self):
        """Load race config from disk."""
        self.contests.clear()

        cr = jsonconfig.config({
            u'event': {
                u'id': EVENT_ID,
                u'contests': [],
                u'timerstat': None,
                u'showinfo': True,
                u'comment': u'',
                u'autospec': u''
            },
            u'contests': {}
        })
        cr.add_section(u'event')
        cr.add_section(u'contests')
        try:
            with open(self.configpath, 'rb') as f:
                cr.read(f)
        except Exception as e:
            self.log.error(u'Reading event config: ' + unicode(e))

        # event metas
        self.info_expand.set_expanded(
            strops.confopt_bool(cr.get(u'event', u'showinfo')))
        self.autospec = cr.get(u'event', u'autospec')
        self.comment = cr.get(u'event', u'comment')
        self.onestart = False

        # restore contests
        oft = 0
        curactive = -1
        for c in cr.get(u'event', u'contests'):
            if cr.has_option(u'contests', c):
                res = cr.get(u'contests', c)
                ft = tod.mktod(res[4])
                if ft or res[5]:
                    self.onestart = True  # at least one run so far
                else:
                    if curactive == -1:
                        curactive = oft
                astr = ''
                if res[0]:
                    astr = self.rider_name(res[0])
                bstr = ''
                if res[2]:
                    bstr = self.rider_name(res[2])
                nr = [
                    c, res[0], astr, res[1], res[2], bstr, res[3], ft, res[5],
                    res[6]
                ]
                self.add_contest(c, nr)
            else:
                self.add_contest(c)
            oft += 1

        if not self.onestart and self.autospec:
            self.del_riders()
            self.meet.autostart_riders(self, self.autospec)

        self.current_contest_combo.set_active(curactive)

        eid = cr.get('event', 'id')
        if eid and eid != EVENT_ID:
            self.log.error('Event configuration mismatch: ' + repr(eid) +
                           ' != ' + repr(EVENT_ID))
            #self.readonly = True

    def rider_name(self, bib):
        """Return a formated rider name string."""
        # TODO: Cache?
        first = ''
        last = ''
        club = ''
        dbr = self.meet.rdb.getrider(bib, self.series)
        if dbr is not None:
            first = self.meet.rdb.getvalue(dbr, 1)
            last = self.meet.rdb.getvalue(dbr, 2)
            club = self.meet.rdb.getvalue(dbr, 3)
        return strops.resname(first, last, club)

    def del_riders(self):
        """Remove all starters from model."""
        for c in self.contests:
            for col in [
                    COL_A_NO, COL_A_STR, COL_A_PLACE, COL_B_NO, COL_B_STR,
                    COL_B_PLACE, COL_WINNER
            ]:
                c[col] = u''
                c[COL_200M] = None

    def add_contest(self, c, cv=[]):
        #if c in self.contests:	# already in event	# requires scan tbl
        #self.log.info('Ignored duplicate contest: ' + repr(c))
        #return None
        if len(cv) == 10:
            self.contests.append(cv)
        else:
            self.contests.append([c, '', '', '', '', '', '', None, '', ''])

    def race_ctrl_action_activate_cb(self, entry, data=None):
        """Perform current action on bibs listed."""
        rlist = entry.get_text()
        acode = self.action_model.get_value(
            self.ctrl_action_combo.get_active_iter(), 1)
        if acode == 'add':
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
        elif acode == 'com':  # add comment to contest
            self.add_comment(rlist)
        else:
            self.log.error('Ignoring invalid action.')
            return False
        glib.idle_add(self.delayed_announce)

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
        if self.event[u'type'] == 'sprint final':
            sec = report.sprintfinal()
        else:
            sec = report.sprintround()
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

        for cr in self.contests:
            byeflag = None
            bno = cr[COL_B_NO]
            bname = cr[COL_B_STR].decode('utf-8')
            timestr = None
            if cr[COL_CONTEST] == u'bye':
                timestr = u' '
                bno = u' '
                bname = u' '
                byeflag = u' '
            if self.event[u'type'] == 'sprint final':
                ## HACK: assume no bye
                sec.lines.append([
                    cr[COL_CONTEST] + u':',
                    [
                        None, cr[COL_A_NO], cr[COL_A_STR].decode('utf-8'),
                        None, None, None, None
                    ],
                    [
                        None, cr[COL_B_NO], cr[COL_B_STR].decode('utf-8'),
                        None, None, None, None
                    ]
                ])
            else:
                sec.lines.append([
                    cr[COL_CONTEST] + u':',
                    [None, cr[COL_A_NO], cr[COL_A_STR].decode('utf-8'), None],
                    [byeflag, bno, bname, None], timestr
                ])
        ret.append(sec)
        return ret

    def saveconfig(self):
        """Save race to disk."""
        if self.readonly:
            self.log.error('Attempt to save readonly ob.')
            return
        cw = jsonconfig.config()
        cw.add_section(u'event')
        cw.add_section(u'contests')

        cw.set(u'event', u'showinfo', self.info_expand.get_expanded())
        cw.set(u'event', u'timerstat', self.timerstat)
        cw.set(u'event', u'comment', self.comment)
        cw.set(u'event', u'autospec', self.autospec)
        contestlist = []
        for c in self.contests:
            cid = c[COL_CONTEST].decode('utf-8')
            contestlist.append(cid)
            ft = None
            if c[COL_200M]:
                ft = c[COL_200M].rawtime()
            cw.set(u'contests', cid, [
                c[COL_A_NO], c[COL_A_PLACE], c[COL_B_NO], c[COL_B_PLACE], ft,
                c[COL_WINNER], c[COL_COMMENT]
            ])
        cw.set(u'event', u'contests', contestlist)
        cw.set(u'event', u'id', EVENT_ID)
        self.log.debug('Saving race config to: ' + self.configpath)
        with open(self.configpath, 'wb') as f:
            cw.write(f)

    def shutdown(self, win=None, msg='Exiting'):
        """Terminate race object."""
        self.log.debug(u'Race shutdown: ' + msg)  # only ref'd local
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def do_properties(self):
        """Run race properties dialog."""
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'sprnd_properties.ui'))
        dlg = b.get_object('properties')
        dlg.set_transient_for(self.meet.window)
        ## FIX
        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            self.log.debug(u'Updating race properties.')
            glib.idle_add(self.delayed_announce)
        else:
            self.log.debug(u'Edit race properties cancelled.')

        # if prefix is empty, grab input focus
        if self.prefix_ent.get_text() == u'':
            self.prefix_ent.grab_focus()
        dlg.destroy()

    def resettimer(self):
        """Reset race timer."""
        self.finish = None
        self.start = None
        self.lstart = None
        self.curelap = None
        self.timerstat = 'idle'
        self.meet.timer.dearm(self.startchan)
        self.meet.timer.dearm(0)
        self.meet.timer.dearm(self.finchan)
        uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Idle')
        self.stat_but.set_sensitive(True)
        self.set_elapsed()

    def setrunning(self):
        """Set timer state to 'running'."""
        self.timerstat = 'running'
        uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Running')

    def setfinished(self):
        """Set timer state to 'finished'."""
        self.timerstat = 'finished'
        uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Finished')
        self.stat_but.set_sensitive(False)

    def armstart(self):
        """Toggle timer arm start state."""
        if self.timerstat == 'idle':
            self.timerstat = 'armstart'
            uiutil.buttonchg(self.stat_but, uiutil.bg_armstart, 'Arm Start')
            self.meet.timer.arm(self.startchan)
            self.meet.timer.arm(0)
        elif self.timerstat == 'armstart':
            self.timerstat = 'idle'
            self.time_lbl.set_text('')
            uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Idle')
            self.meet.timer.dearm(self.startchan)
            self.meet.timer.dearm(0)
        return False  # for use in delayed callback

    def armfinish(self):
        """Toggle timer arm finish state."""
        if self.timerstat == 'running':
            self.timerstat = 'armfinish'
            uiutil.buttonchg(self.stat_but, uiutil.bg_armfin, 'Arm Finish')
            self.meet.timer.arm(self.finchan)
        elif self.timerstat == 'armfinish':
            self.timerstat = 'running'
            uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Running')
            self.meet.timer.dearm(self.finchan)
        return False  # for use in delayed callback

    def showtimer(self):
        """Display the running time on the scoreboard."""
        if self.timerstat == 'idle':
            self.armstart()
        ## NOTE: display todo
        tp = '200m:'
        self.meet.scbwin = scbwin.scbtimer(self.meet.scb, self.event[u'pref'],
                                           self.event[u'info'], tp)
        self.timerwin = True
        self.meet.scbwin.reset()
        self.meet.gemini.reset_fields()
        if self.timerstat == 'finished':
            if self.start is not None and self.finish is not None:
                elap = self.finish - self.start
                self.meet.scbwin.settime(elap.timestr(2))
                self.meet.scbwin.setavg(elap.speedstr(200))  # fixed dist
                self.meet.gemini.set_time(elap.rawtime(2))
            self.meet.scbwin.update()
        self.meet.gemini.show_brt()

    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or 'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_abort:  # override ctrl+f5
                    self.resettimer()
                    return True
                elif key == key_walk_a:
                    self.set_winner(u'A', wplace=u'w/o', lplace=u'dns')
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_walk_b:
                    self.set_winner(u'B', wplace=u'w/o', lplace=u'dns')
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_rel_a:
                    self.set_winner(u'B', wplace=u'1.', lplace=u'rel')
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_rel_b:  # rel B => A wins
                    self.set_winner(u'A', wplace='1.', lplace='rel')
                    glib.idle_add(self.delayed_announce)
                    return True
                # TODO: next/prev contest
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
                    self.doscbplaces = True  # override if already clear
                    self.redo_places()
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_win_a:
                    self.set_winner(u'A')
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_win_b:
                    self.set_winner(u'B')
                    glib.idle_add(self.delayed_announce)
                    return True
        return False

    def set_winner(self, win, wplace=u'1.', lplace=u'2.'):
        i = self.current_contest_combo.get_active_iter()
        if i is not None:  # contest selected ok
            prevwin = self.contests.get_value(i, COL_WINNER)
            cid = self.contests.get_value(i, COL_CONTEST)
            if prevwin:  # warn override
                self.log.info('Overwriting contest winner: ' + repr(prevwin))
            wno = u''
            wstr = u''
            lno = u''
            lstr = u''
            fstr = u''
            ft = self.contests.get_value(i, COL_200M)
            if ft is not None:
                fstr = ft.rawtime(2)
            if win == 'A':
                self.contests.set_value(i, COL_A_PLACE, wplace)
                self.contests.set_value(i, COL_B_PLACE, lplace)
                wno = self.contests.get_value(i, COL_A_NO)
                wstr = self.contests.get_value(i, COL_A_STR).decode('utf-8')
                lno = self.contests.get_value(i, COL_B_NO)
                lstr = self.contests.get_value(i, COL_B_STR).decode('utf-8')
                self.contests.set_value(i, COL_WINNER, wno)
            else:
                self.contests.set_value(i, COL_B_PLACE, wplace)
                self.contests.set_value(i, COL_A_PLACE, lplace)
                wno = self.contests.get_value(i, COL_B_NO)
                wstr = self.contests.get_value(i, COL_B_STR).decode('utf-8')
                lno = self.contests.get_value(i, COL_A_NO)
                lstr = self.contests.get_value(i, COL_A_STR).decode('utf-8')
                self.contests.set_value(i, COL_WINNER, wno)
            if not prevwin:
                self.do_places(cid, wno, wstr, wplace, lno, lstr, lplace, fstr)
                self.meet.gemini.set_bib(wno)
                self.meet.gemini.set_time(fstr.strip().rjust(4) + ' ')
                self.meet.gemini.show_brt()

    def redo_places(self):
        i = self.current_contest_combo.get_active_iter()
        if i is not None:  # contest selected ok
            cid = self.contests.get_value(i, COL_CONTEST)
            win = self.contests.get_value(i, COL_WINNER)
            wno = u''
            wstr = u''
            wplace = u''
            lno = u''
            lstr = u''
            fstr = u''
            ft = self.contests.get_value(i, COL_200M)
            if ft is not None:
                fstr = ft.rawtime(2)
            if win == 'A':
                wplace = self.contests.get_value(i, COL_A_PLACE)
                lplace = self.contests.get_value(i, COL_B_PLACE)
                wno = self.contests.get_value(i, COL_A_NO)
                wstr = self.contests.get_value(i, COL_A_STR).decode('utf-8')
                lno = self.contests.get_value(i, COL_B_NO)
                lstr = self.contests.get_value(i, COL_B_STR).decode('utf-8')
            else:
                wplace = self.contests.get_value(i, COL_B_PLACE)
                lplace = self.contests.get_value(i, COL_A_PLACE)
                wno = self.contests.get_value(i, COL_B_NO)
                wstr = self.contests.get_value(i, COL_B_STR).decode('utf-8')
                lno = self.contests.get_value(i, COL_A_NO)
                lstr = self.contests.get_value(i, COL_A_STR).decode('utf-8')
            self.do_places(cid, wno, wstr, wplace, lno, lstr, lplace, fstr)

    def do_places(self, contest, winno, winner, winpl, loseno, loser, losepl,
                  ftime):
        """Show contest result on scoreboard."""
        self.meet.scbwin = None
        self.timerwin = False
        startlist = [[u'1.', winno, winner], [u'2.', loseno, loser]]
        if ftime:
            startlist.append([u'', u'', u''])
            startlist.append([u'', u'', u'200m: ' + ftime])
        name_w = self.meet.scb.linelen - 8
        fmt = [(3, 'l'), (4, 'r'), u' ', (name_w, 'l')]
        self.meet.scbwin = scbwin.scbintsprint(
            self.meet.scb, self.meet.racenamecat(self.event), contest, fmt,
            startlist)

        self.meet.announce.gfx_overlay(2)
        self.meet.announce.gfx_set_title(u'Result: ' + self.event[u'pref'] +
                                         u' ' + self.event[u'info'] + u' ' +
                                         contest)
        self.meet.announce.gfx_add_row([winpl, winner, ftime])
        self.meet.announce.gfx_add_row([losepl, loser, ''])

        self.meet.scbwin.reset()

    def do_startlist(self):
        """Show start list on scoreboard."""
        # clear gem board
        self.meet.gemini.reset_fields()
        self.meet.gemini.show_brt()

        # prepare start list board	(use 2+2)
        cid = ''
        startlist = []
        i = self.current_contest_combo.get_active_iter()
        if i is not None:  # contest selected ok
            cid = self.contests.get_value(i, COL_CONTEST)
            self.meet.announce.gfx_overlay(2)
            self.meet.announce.gfx_set_title(u'Startlist: ' +
                                             self.event[u'pref'] + u' ' +
                                             self.event[u'info'] + u' ' + cid)
            an = self.contests.get_value(i, COL_A_NO)
            ar = self.contests.get_value(i, COL_A_STR).decode('utf-8')
            self.meet.announce.gfx_add_row([an, ar, ''])
            startlist.append([an, ar])
            bn = self.contests.get_value(i, COL_B_NO)
            br = self.contests.get_value(i, COL_B_STR).decode('utf-8')
            self.meet.announce.gfx_add_row([bn, br, ''])
            startlist.append([bn, br])
        self.meet.scbwin = None
        self.timerwin = False
        name_w = self.meet.scb.linelen - 5
        fmt = [(4, 'r'), u' ', (name_w, 'l')]
        self.meet.scbwin = scbwin.scbintsprint(
            self.meet.scb, self.meet.racenamecat(self.event), cid, fmt,
            startlist)
        self.meet.scbwin.reset()

    def update_expander_lbl_cb(self):
        """Update race info expander label."""
        self.info_expand.set_label('Race Info : ' +
                                   self.meet.racenamecat(self.event, 64))

    def editent_cb(self, entry, col):
        """Shared event entry update callback."""
        if col == u'pref':
            self.event[u'pref'] = entry.get_text().decode('utf-8', 'replace')
        elif col == u'info':
            self.event[u'info'] = entry.get_text().decode('utf-8', 'replace')
        self.update_expander_lbl_cb()

    def starttrig(self, e):
        """React to start trigger."""
        if self.timerstat == 'armstart':
            self.start = e
            self.lstart = tod.now()
            self.setrunning()
            glib.timeout_add_seconds(4, self.armfinish)

    def fintrig(self, e):
        """React to finish trigger."""
        if self.timerstat == 'armfinish':
            self.finish = e
            self.setfinished()
            self.set_elapsed()
            cid = ''
            i = self.current_contest_combo.get_active_iter()
            if i is not None:  # contest selected ok
                cid = self.contests.get_value(i, COL_CONTEST)
                self.contests.set_value(i, COL_200M, self.curelap)
                self.ctrl_winner.grab_focus()
            self.log_elapsed(cid)
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtimer:
                self.showtimer()
                if self.start is not None:
                    self.meet.gemini.rtick(self.finish - self.start, 2)
            glib.idle_add(self.delayed_announce)

    def rftimercb(self, e):
        """Handle a RF timer event."""
        return False  # todo... auto win if thresh met

    def timercb(self, e):
        """Handle a timer event."""
        chan = timy.chan2id(e.chan)
        if chan == self.startchan or chan == 0:
            self.log.debug(u'Got a start impulse.')
            self.starttrig(e)
        elif chan == self.finchan:
            self.log.debug(u'Got a finish impulse.')
            self.fintrig(e)
        return False

    def timeout(self):
        """Update scoreboard and respond to timing events."""
        if not self.winopen:
            return False
        if self.finish is None:
            self.set_elapsed()
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtimer:
                elapstr = self.time_lbl.get_text()
                self.meet.scbwin.settime(elapstr)
                self.meet.gemini.set_time(elapstr.strip().rjust(4) + ' ')
                self.meet.gemini.show_brt()
        return True

    def set_start(self, start='', lstart=None):
        """Set the race start."""
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
            pass
        else:
            if self.finish is None:
                self.setrunning()

    def log_elapsed(self, contest=''):
        """Log race elapsed time on Timy."""
        if contest:
            self.meet.timer.printline(u'Ev ' + self.evno + u' [' + contest +
                                      u']')
        self.meet.timer.printline(u'      ST: ' + self.start.timestr(4))
        self.meet.timer.printline(u'     FIN: ' + self.finish.timestr(4))
        self.meet.timer.printline(u'    TIME: ' +
                                  (self.finish - self.start).timestr(2))

    def set_finish(self, finish=''):
        """Set the race finish."""
        if type(finish) is tod.tod:
            self.finish = finish
        else:
            self.finish = tod.mktod(finish)
        if self.finish is None:
            if self.start is not None:
                self.setrunning()
        else:
            if self.start is None:
                self.set_start('0')
            self.setfinished()

    def set_elapsed(self):
        """Update elapsed time in race ui and announcer."""
        self.curelap = None
        if self.start is not None and self.finish is not None:
            et = self.finish - self.start
            self.time_lbl.set_text(et.timestr(2))
            self.curelap = et
        elif self.start is not None:  # Note: uses 'local start' for RT
            runtm = (tod.now() - self.lstart).timestr(1)
            self.time_lbl.set_text(runtm)
        elif self.timerstat == 'armstart':
            self.time_lbl.set_text(tod.tod(0).timestr(1))
        else:
            self.time_lbl.set_text('')

    def current_contest_combo_changed_cb(self, combo, data=None):
        """Copy elapsed time into timer (dodgey)."""
        self.resettimer()
        i = self.current_contest_combo.get_active_iter()
        if i is not None:  # contest selected ok
            ft = self.contests.get_value(i, COL_200M)
            if ft is not None:
                self.start = tod.tod(0)
                self.finish = ft
                self.set_elapsed()
            else:
                self.start = None
                self.finish = None
                self.set_elapsed()
            winner = self.contests.get_value(i, COL_WINNER)
            self.ctrl_winner.set_text(winner)

    def race_ctrl_winner_activate_cb(self, entry, data=None):
        """Manual entry of race winner."""
        winner = entry.get_text().decode('ascii', 'ignore')
        i = self.current_contest_combo.get_active_iter()
        if i is not None:  # contest selected ok
            cid = self.contests.get_value(i, COL_CONTEST)
            self.ctrl_winner.grab_focus()
            ano = self.contests.get_value(i, COL_A_NO)
            bno = self.contests.get_value(i, COL_B_NO)
            if winner == ano:
                self.set_winner(u'A')
                glib.idle_add(self.delayed_announce)
            elif winner == bno:
                self.set_winner(u'B')
                glib.idle_add(self.delayed_announce)
            else:
                self.log.error(u'Ignored rider not in contest.')
        else:
            self.log.info(u'No contest selected.')

    def race_info_time_edit_activate_cb(self, button):
        """Display contest timing edit dialog."""
        ostx = ''
        oftx = ''
        if self.start is not None:
            ostx = self.start.rawtime(4)
        else:
            ostx = '0.0'
        if self.finish is not None:
            oftx = self.finish.rawtime(4)
        ret = uiutil.edit_times_dlg(self.meet.window, ostx, oftx)
        if ret[0] == 1:
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
                cid = ''
                i = self.current_contest_combo.get_active_iter()
                if i is not None:  # contest selected ok
                    cid = self.contests.get_value(i, COL_CONTEST)
                    self.contests.set_value(i, COL_200M, self.curelap)
                if self.start is not None and self.finish is not None:
                    self.log_elapsed(cid)
                self.log.info('Updated race times.')
            except Exception as v:
                self.log.error('Error updating times: ' + str(v))

            glib.idle_add(self.delayed_announce)
        else:
            self.log.info('Edit race times cancelled.')

    def delayed_announce(self):
        """Initialise the announcer's screen after a delay."""
        if self.winopen:
            self.meet.announce.clrall()
            self.meet.ann_title(' '.join([
                self.meet.event_string(self.evno), ':', self.event[u'pref'],
                self.event[u'info']
            ]))
            lapstring = strops.lapstring(self.event[u'laps'])
            substr = u' '.join(
                [lapstring, self.event[u'dist'], self.event[u'prog']]).strip()
            if substr:
                self.meet.announce.postxt(1, 0, substr.center(80))
            self.meet.announce.linefill(2, '_')
            self.meet.announce.linefill(8, '_')
            # announce current contest
            i = self.current_contest_combo.get_active_iter()
            if i is not None:  # contest selected ok
                cid = self.contests.get_value(i, COL_CONTEST)
                self.meet.announce.postxt(4, 0, u'Contest: ' + cid)
                ano = self.contests.get_value(i, COL_A_NO).rjust(3)
                astr = self.contests.get_value(i, COL_A_STR).decode('utf-8')
                aplace = self.contests.get_value(i, COL_A_PLACE).ljust(3)
                bni = self.contests.get_value(i, COL_B_NO)
                bno = bni.rjust(3)
                bstr = self.contests.get_value(i, COL_B_STR).decode('utf-8')
                bplace = self.contests.get_value(i, COL_B_PLACE).ljust(3)
                if self.contests.get_value(i, COL_WINNER) == bni:
                    self.meet.announce.postxt(
                        6, 0, bplace + u' ' + bno + u' ' + bstr)
                    self.meet.announce.postxt(
                        7, 0, aplace + u' ' + ano + u' ' + astr)
                else:
                    self.meet.announce.postxt(
                        6, 0, aplace + u' ' + ano + u' ' + astr)
                    self.meet.announce.postxt(
                        7, 0, bplace + u' ' + bno + u' ' + bstr)
                ft = self.contests.get_value(i, COL_200M)
                if ft is not None:
                    self.meet.announce.postxt(
                        6, 60, u'200m: ' + ft.rawtime(2).rjust(10))
                    self.meet.announce.postxt(
                        7, 60, u' Avg: ' + ft.speedstr().strip().rjust(10))
            # show 'leaderboard'
            lof = 10
            for c in self.contests:
                cid = (c[COL_CONTEST] + u':').ljust(8)
                win = c[COL_WINNER]
                lr = u''
                rr = u''
                sep = u' v '
                if win:
                    if c[COL_CONTEST] == u'bye':
                        sep = '   '
                    else:
                        sep = 'def'
                if win == c[COL_B_NO]:
                    lr = (c[COL_B_NO].rjust(3) + u' ' +
                          strops.truncpad(c[COL_B_STR].decode('utf-8'), 29))
                    rr = (c[COL_A_NO].rjust(3) + u' ' +
                          strops.truncpad(c[COL_A_STR].decode('utf-8'), 29))
                else:
                    lr = (c[COL_A_NO].rjust(3) + u' ' +
                          strops.truncpad(c[COL_A_STR].decode('utf-8'), 29))
                    rr = (c[COL_B_NO].rjust(3) + u' ' +
                          strops.truncpad(c[COL_B_STR].decode('utf-8'), 29))
                self.meet.announce.postxt(lof, 0, u' '.join([cid, lr, sep,
                                                             rr]))
                lof += 1

        return False

    def result_gen(self):
        """Generator function to export a final result."""
        cstack = []
        placeoft = 1
        for c in self.contests:
            rank = None
            time = None
            info = None  # rel/dsq/etc?
            win = c[COL_A_NO]
            lose = c[COL_B_NO]
            lr = False
            if c[COL_WINNER]:
                rank = placeoft
                win = c[COL_WINNER]
                if lose == win:  # win went to 'B' rider
                    lose = c[COL_A_NO]
                if c[COL_200M] is not None:
                    time = c[COL_200M].truncate(2)  # valid?
                lr = True  # include rank on loser rider
            cstack.insert(0, (lose, lr))
            time = None
            yield [win, rank, time, info]
            placeoft += 1
        for (bib, lr) in cstack:
            rank = None
            time = None
            info = None  # rel/dsq/etc?
            if lr:
                rank = placeoft
            yield [bib, rank, time, info]
            placeoft += 1

    def result_report(self, recurse=False):
        """Return a list of report sections containing the race result."""
        ret = []
        sec = report.sprintround()
        sec.heading = u'Event ' + self.evno + u': ' + u' '.join(
            [self.event[u'pref'], self.event[u'info']]).strip()
        sec.lines = []
        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join(
            [lapstring, self.event[u'dist'], self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr

        for cr in self.contests:
            # if winner set, report a result, otherwise, use startlist style:
            cprompt = cr[COL_CONTEST] + u':'
            if cr[COL_WINNER]:
                avec = [
                    cr[COL_A_PLACE], cr[COL_A_NO],
                    cr[COL_A_STR].decode('utf-8'), None
                ]
                bvec = [
                    cr[COL_B_PLACE], cr[COL_B_NO],
                    cr[COL_B_STR].decode('utf-8'), None
                ]
                ft = None
                if cr[COL_200M] is not None:
                    ft = cr[COL_200M].rawtime(2)
                else:
                    ft = u' '
                if cr[COL_WINNER] == cr[COL_A_NO]:
                    sec.lines.append([cprompt, avec, bvec, ft])
                else:
                    sec.lines.append([cprompt, bvec, avec, ft])
            else:
                sec.lines.append([
                    cprompt,
                    [None, cr[COL_A_NO], cr[COL_A_STR].decode('utf-8'), None],
                    [None, cr[COL_B_NO], cr[COL_B_STR].decode('utf-8'), None],
                    None
                ])
        ret.append(sec)

        if self.comment:
            sec = report.bullet_text()
            sec.subheading = u'Decisions of the commisaires panel'
            sec.lines.append([None, self.comment])
            ret.append(sec)

        return ret

    def todstr(self, col, cr, model, iter, data=None):
        """Format tod into text for listview."""
        ft = model.get_value(iter, COL_200M)
        if ft is not None:
            cr.set_property('text', ft.rawtime(2))
        else:
            cr.set_property('text', u'')

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
        self.configpath = meet.event_configfile(self.evno)

        self.log = logging.getLogger('sprintround')
        self.log.setLevel(logging.DEBUG)  # config may override?
        self.log.debug(u'Creating new event: ' + repr(self.evno))

        self.readonly = not ui
        self.onestart = False
        self.start = None
        self.lstart = None
        self.finish = None
        self.curelap = None
        self.winopen = ui  # window 'open' on proper load- or consult edb
        self.timerwin = False
        self.timerstat = 'idle'
        self.autospec = ''  # automatic startlist
        self.inomnium = False
        self.startchan = 4
        self.finchan = 1
        self.contests = []
        self.comment = u''

        self.contests = gtk.ListStore(
            gobject.TYPE_STRING,  # COL_CONTEST = 0
            gobject.TYPE_STRING,  # COL_A_NO = 1
            gobject.TYPE_STRING,  # COL_A_STR = 2
            gobject.TYPE_STRING,  # COL_A_PLACE = 3
            gobject.TYPE_STRING,  # COL_B_NO = 4
            gobject.TYPE_STRING,  # COL_B_STR = 5
            gobject.TYPE_STRING,  # COL_B_PLACE = 6
            gobject.TYPE_PYOBJECT,  # COL_200M = 7
            gobject.TYPE_STRING,  # COL_WINNER = 8
            gobject.TYPE_STRING  # COL_COMMENT = 9
        )

        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'sprnd.ui'))

        self.frame = b.get_object('race_vbox')
        self.frame.connect('destroy', self.shutdown)

        # info pane
        self.info_expand = b.get_object('info_expand')
        b.get_object('race_info_evno').set_text(self.evno)
        self.showev = b.get_object('race_info_evno_show')
        self.prefix_ent = b.get_object('race_info_prefix')
        self.prefix_ent.connect('changed', self.editent_cb, u'pref')
        self.prefix_ent.set_text(self.event[u'pref'])
        self.info_ent = b.get_object('race_info_title')
        self.info_ent.connect('changed', self.editent_cb, u'info')
        self.info_ent.set_text(self.event[u'info'])

        self.time_lbl = b.get_object('race_info_time')
        self.time_lbl.modify_font(pango.FontDescription("monospace"))
        self.type_lbl = b.get_object('race_type')
        self.type_lbl.set_text(self.event[u'type'].capitalize())

        # ctrl pane
        self.stat_but = b.get_object('race_ctrl_stat_but')
        self.ctrl_winner = b.get_object('race_ctrl_winner')
        self.ctrl_action_combo = b.get_object('race_ctrl_action_combo')
        self.ctrl_action = b.get_object('race_ctrl_action')
        self.action_model = b.get_object('race_action_model')

        self.current_contest_combo = b.get_object('current_contest_combo')
        self.current_contest_combo.set_model(self.contests)
        self.current_contest_combo.connect(
            'changed', self.current_contest_combo_changed_cb)

        # riders pane
        t = gtk.TreeView(self.contests)
        self.view = t
        t.set_reorderable(False)
        t.set_enable_search(False)
        t.set_rules_hint(True)

        # riders columns
        uiutil.mkviewcoltxt(t, 'Contest', COL_CONTEST)
        uiutil.mkviewcoltxt(t, '', COL_A_NO, calign=1.0)
        uiutil.mkviewcoltxt(t, 'A Rider', COL_A_STR, expand=True)
        uiutil.mkviewcoltxt(t, '', COL_B_NO, calign=1.0)
        uiutil.mkviewcoltxt(t, 'B Rider', COL_B_STR, expand=True)
        uiutil.mkviewcoltod(t, '200m', cb=self.todstr)
        uiutil.mkviewcoltxt(t, 'Win', COL_WINNER)
        t.show()
        b.get_object('race_result_win').add(t)

        # start timer and show window
        if ui:
            # connect signal handlers
            b.connect_signals(self)
