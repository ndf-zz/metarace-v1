"""DO NOT USE"""

import os
import logging
import gtk
import glib
import gobject

import metarace
from metarace import scbwin
from metarace import jsonconfig
from metarace import tod
from metarace import uiutil
from metarace import eventdb
from metarace import riderdb
from metarace import strops
from metarace import report

_log = logging.getLogger(u'metarace.aggregate')
_log.setLevel(logging.DEBUG)

# config version string
EVENT_ID = u'agg-2.0'

# Model columns (for the view only)
COL_BIB = 0
COL_NAME = 1
COL_CLUB = 2
COL_CAT = 3
COL_SEX = 4
COL_POINTS = 5

# scb function key mappings
key_reannounce = u'F4'  # (+CTRL)
key_abort = u'F5'  # (+CTRL)
key_startlist = u'F3'
key_results = u'F4'


class aggregate(object):

    def loadconfig(self):
        """Load race config from disk."""
        cr = jsonconfig.config({
            u'event': {
                u'id': EVENT_ID,
                u'showinfo': True,
                u'comments': [],
                u'pointspec': u'',
                u'events': u'',
                u'double': u'',
                u'qualifying': u''
            }
        })
        cr.add_section('event')
        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                _log.error(u'Unable to read config: %s', e)
        else:
            _log.info(u'%r not found, loading defaults', self.configfile)

        self.update_expander_lbl_cb()
        self.info_expand.set_expanded(
            strops.confopt_bool(cr.get(u'event', u'showinfo')))

        self.pointspec = cr.get(u'event', u'pointspec')
        self.events = cr.get(u'event', u'events')
        self.double = cr.get(u'event', u'double')
        self.qualifying = cr.get(u'event', u'qualifying')
        self.recalculate()  # model is cleared and loaded in recalc
        eid = cr.get(u'event', u'id')
        if eid and eid != EVENT_ID:
            _log.info(u'Event config mismatch: %r != %r', eid, EVENT_ID)

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
        return ret
        # Don't do a startlist for agg entries

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
        cw.set(u'event', u'pointspec', self.pointspec)
        cw.set(u'event', u'events', self.events)
        cw.set(u'event', u'double', self.double)
        cw.set(u'event', u'qualifying', self.qualifying)
        cw.set(u'event', u'comments', self.comments)
        cw.set(u'event', u'showinfo', self.info_expand.get_expanded())
        cw.set(u'event', u'id', EVENT_ID)
        _log.debug(u'Saving event config %r', self.configfile)
        with metarace.savefile(self.configfile) as f:
            cw.write(f)

    def result_gen(self):
        """Generator function to export a final result."""
        # don't allow result propagation for aggregate (yet?)
        for r in []:
            yield ['', '', None, None]

    def result_report(self, recurse=True):  # by default include inners
        """Return a list of report sections containing the race result."""
        ret = []
        after = u''
        if self.afterev is not None:
            after = ' After Event {}'.format(self.afterev)

        # Club Result (with averages) - mark avg winner with chevron
        sec = report.section(u'teams')
        if self.event[u'evov']:
            sec.heading = u' '.join([self.event[u'pref'],
                                     self.event[u'info']]).strip()
        else:
            sec.heading = u'Event ' + self.evno + u': ' + u' '.join(
                [self.event[u'pref'], self.event[u'info']]).strip()
        sec.subheading = u'Team Tally' + after
        sec.lines = self.teamres
        sec.colheader = [None, None, None, None, u'Avg', u'Total']
        sec.units = u'pt'
        ret.append(sec)

        # Individual Tally - mark sex and cat
        sec = report.section(u'individual')
        sec.subheading = u'Individual Tally' + after
        sec.lines = self.riderres
        sec.units = u'pt'
        ret.append(sec)

        return ret

        # start with the overall result
        sec = report.section()
        if self.event[u'evov']:
            sec.heading = u' '.join([self.event[u'pref'],
                                     self.event[u'info']]).strip()
        else:
            sec.heading = u'Event ' + self.evno + u': ' + u' '.join(
                [self.event[u'pref'], self.event[u'info']]).strip()
        sec.lines = []
        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join(
            [lapstring, self.event[u'dist'], self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr
        prevmedal = u''

    def addrider(self, bib=u'', place=u''):
        """Add specified rider to race model."""
        # note this is not relevant for aggregate
        return None

    def getrider(self, bib):
        """Return temporary reference to model row."""
        # note this is not relevant for aggregate
        return None

    def delrider(self, bib):
        """Remove the specified rider from the model."""
        # note this is not relevant for aggregate
        return None

    def getiter(self, bib):
        """Return temporary iterator to model row."""
        # note this is not relevant for aggregate
        return None

    def recalculate(self):
        """Update internal model."""

        # clear out view and data structures
        self.riders.clear()
        self.ridermap = {}
        self.riderres = []
        self.teammap = {}
        self.teamres = []
        self.afterev = None

        # check config (?)
        events = self.events.split()
        doubles = self.double.split()
        points = [int(i) for i in self.pointspec.split()]

        # collect points and allocate to riders and teams
        for evno in events:
            r = self.meet.get_event(evno, False)
            if r is None:
                _log.warning(u'Event %r not found', evno)
                return
            r.loadconfig()  # now have queryable event handle
            double = False
            if evno in doubles:
                double = True
            for res in r.result_gen():
                if isinstance(res[1], int):
                    self.afterev = evno
                    oft = res[1] - 1
                    pts = 0
                    if oft < len(points):
                        pts = points[oft]
                    else:
                        _log.debug(u'More places than points: %r > %r', oft,
                                   points)
                    if double:
                        pts += pts

                    riderno = res[0]
                    if riderno not in self.ridermap:
                        name = u''
                        club = u''
                        namestr = u''
                        cat = u''
                        sex = u''
                        rh = self.meet.newgetrider(riderno)
                        if rh is not None:
                            name = rh[u'name']
                            club = rh[u'club']
                            namestr = rh[u'namestr']
                            cat = rh[u'cat']
                            sex = rh[u'ucicode']
                        self.ridermap[riderno] = {
                            u'name': name,
                            u'club': club,
                            u'namestr': namestr,
                            u'cat': cat,
                            u'sex': sex,
                            u'points': 0
                        }
                    self.ridermap[riderno][u'points'] += pts
                    club = self.ridermap[riderno][u'club']
                    if club not in self.teammap:
                        self.teammap[club] = {
                            u'name': club,
                            u'riders': set(),
                            u'points': 0,
                        }
                    self.teammap[club][u'riders'].add(riderno)
                    self.teammap[club][u'points'] += pts

        # create an aux sorting for overall points
        rideraux = []
        for rider in self.ridermap:
            pts = self.ridermap[rider][u'points']
            riderval = strops.bibstr_key(rider)
            rideraux.append((-pts, riderval, rider))
        rideraux.sort()
        for r in rideraux:
            rno = r[2]
            rider = self.ridermap[rno]
            self.riders.append([
                rno, rider[u'name'], rider[u'club'], rider[u'cat'],
                rider[u'sex'],
                unicode(rider[u'points'])
            ])
            # also write to result thingo
            self.riderres.append([
                u'', rno, rider[u'namestr'], rider[u'sex'] + rider[u'cat'],
                u'',
                unicode(rider[u'points'])
            ])

        # creat aux sorting for team points
        bestteam = None
        bestavg = 0
        teamaux = []
        for team in self.teammap:
            pts = self.teammap[team][u'points']
            count = len(self.teammap[team][u'riders'])
            avg = float(pts) / count
            self.teammap[team][u'average'] = avg
            if avg > bestavg:
                bestteam = team
                bestavg = avg
            teamaux.append((-pts, -avg, team))
        teamaux.sort()
        for t in teamaux:
            name = t[2]
            team = self.teammap[name]
            pstr = unicode(team[u'points'])
            star = u''
            if name == bestteam:
                star = u'\u2605 '
            astr = '{0}{1:0.1f}'.format(star, team[u'average'])
            self.teamres.append([u'', u'', name, u'', astr, pstr])

        # update timerstat
        # if all results:
        #  self.timerstat = u'result'
        # if any results...
        #  self.timerstat = u'standing'
        # else:
        #  self.timerstat = u'idle'

        return

    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or u'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_abort or key == key_reannounce:
                    # override ctrl+f5
                    self.recalculate()
                    glib.idle_add(self.delayed_announce)
                    return True
            elif key[0] == u'F':
                if key == key_startlist:
                    self.do_startlist()
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_results:
                    self.do_places()
                    glib.idle_add(self.delayed_announce)
                    return True
        return False

    def delayed_announce(self):
        """Initialise the announcer's screen after a delay."""
        ## TODO because # riders often exceeds 24 - requires paging
        return False
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
            lmedal = ''
            posoft = 0
            l = 4
            for r in self.riders:
                if l > 17:
                    l = 4
                    posoft += 41
                plstr = u''
                pls = r[COL_PLACE].decode(u'utf-8')
                if pls:
                    plstr = pls
                    if plstr.isdigit():
                        plstr += u'.'
                plstr = strops.truncpad(plstr, 3, u'l')
                bibstr = strops.truncpad(r[COL_BIB].decode(u'utf-8'), 3, u'r')
                clubstr = u''
                tcs = r[COL_CLUB].decode(u'utf-8')
                if tcs and tcs <= 3:
                    clubstr = u' (' + tcs + u')'
                namestr = strops.truncpad(
                    strops.fitname(r[COL_FIRST].decode(u'utf-8'),
                                   r[COL_LAST].decode(u'utf-8'),
                                   25 - len(clubstr)) + clubstr, 25)
                medal = r[COL_MEDAL].decode(u'utf-8')
                if lmedal != u'' and medal == u'':
                    l += 1  # gap to medals
                lmedal = medal
                ol = [plstr, bibstr, namestr, medal]
                self.meet.txt_postxt(
                    l, posoft, u' '.join([plstr, bibstr, namestr, medal]))
                l += 1

        return False

    def do_startlist(self):
        """Show result on scoreboard."""
        # Don't show for agg
        return False

    def do_places(self):
        """Show race result on scoreboard."""
        # Don't show for agg
        return False
        ## todo: DISPLAY CLUB STANDINGS
        resvec = []
        count = 0
        teamnames = False
        name_w = self.meet.scb.linelen - 12
        fmt = [(3, u'l'), (4, u'r'), u' ', (name_w, u'l'), (4, u'r')]
        if self.series and self.series[0].lower() == u't':
            teamnames = True
            name_w = self.meet.scb.linelen - 8
            fmt = [(3, u'l'), u' ', (name_w, u'l'), (4, u'r')]

        for r in self.riders:
            plstr = r[COL_PLACE].decode('utf-8')
            if plstr.isdigit():
                plstr = plstr + u'.'
            no = r[COL_BIB]
            first = r[COL_FIRST].decode('utf-8')
            last = r[COL_LAST].decode('utf-8')
            club = r[COL_CLUB].decode('utf-8')
            if not teamnames:
                resvec.append(
                    [plstr, no,
                     strops.fitname(first, last, name_w), club])
            else:
                resvec.append([plstr, first, club])
            count += 1
        self.meet.scbwin = None
        header = self.meet.racenamecat(self.event)
        ## TODO: Flag Provisional
        evtstatus = u'Final Classification'.upper()
        self.meet.scbwin = scbwin.scbtable(scb=self.meet.scb,
                                           head=self.meet.racenamecat(
                                               self.event),
                                           subhead=evtstatus,
                                           coldesc=fmt,
                                           rows=resvec)
        self.meet.scbwin.reset()
        return False

    def shutdown(self, win=None, msg='Exiting'):
        """Terminate race object."""
        _log.debug('Shutdown event %s: %s', self.evno, msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def timercb(self, e):
        """Handle a timer event."""
        return False

    def timeout(self):
        """Update scoreboard and respond to timing events."""
        if not self.winopen:
            return False
        return True

    def do_properties(self):
        """Run race properties dialog."""
        prfile = os.path.join(metarace.UI_PATH, u'aggregate_properties.ui')
        _log.debug(u'Building event properties from %r', prfile)
        b = gtk.Builder()
        b.add_from_file(prfile)
        dlg = b.get_object(u'properties')
        dlg.set_transient_for(self.meet.window)
        se = b.get_object(u'race_qualifying_entry')
        se.set_text(self.qualifying)
        ee = b.get_object(u'race_events_entry')
        ee.set_text(self.events)
        pe = b.get_object(u'race_points_entry')
        pe.set_text(self.pointspec)
        me = b.get_object(u'race_double_entry')
        me.set_text(self.double)
        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            _log.debug(u'Updating event properties')
            self.pointspec = pe.get_text().decode(u'utf-8')
            self.double = me.get_text().decode(u'utf-8')
            self.events = ee.get_text().decode(u'utf-8')
            self.qualifying = se.get_text().decode(u'utf-8')
            self.recalculate()
            glib.idle_add(self.delayed_announce)
        else:
            _log.debug(u'Edit event properties cancelled')

        # if prefix is empty, grab input focus
        if not self.prefix_ent.get_text():
            self.prefix_ent.grab_focus()
        dlg.destroy()

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

    def update_expander_lbl_cb(self):
        """Update race info expander label."""
        self.info_expand.set_label(self.meet.infoline(self.event))

    def editent_cb(self, entry, col):
        """Shared event entry update callback."""
        if col == u'pref':
            self.event[u'pref'] = entry.get_text().decode(u'utf-8')
        elif col == u'info':
            self.event[u'info'] = entry.get_text().decode(u'utf-8')
        self.update_expander_lbl_cb()

    def editcol_db(self, cell, path, new_text, col):
        """Cell update with writeback to meet."""
        new_text = new_text.decode(u'utf-8', 'replace').strip()
        self.riders[path][col] = new_text
        glib.idle_add(self.meet.rider_edit,
                      self.riders[path][COL_BIB].decode(u'utf-8'), self.series,
                      col, new_text)

    def __init__(self, meet, event, ui=True):
        """Constructor."""
        self.meet = meet
        self.event = event  # Note: now a treerowref
        self.evno = event[u'evid']
        self.evtype = event[u'type']
        self.series = event[u'seri']
        self.configfile = meet.event_configfile(self.evno)
        _log.debug(u'Init event %s', self.evno)

        # race run time attributes
        self.onestart = True  # always true for autospec classification
        self.readonly = not ui
        self.winopen = ui
        self.comments = []
        self.events = u''
        self.qualifying = u''
        self.pointspec = u''
        self.double = u''
        self.timerstat = u'idle'  # fake a timerstat and use for result stat
        self.ridermap = {}
        self.teammap = {}
        self.teamres = []
        self.teamavgres = []
        self.afterev = None

        self.riders = gtk.ListStore(
            gobject.TYPE_STRING,  # 0 bib
            gobject.TYPE_STRING,  # 1 name
            gobject.TYPE_STRING,  # 2 club
            gobject.TYPE_STRING,  # 3 cat
            gobject.TYPE_STRING,  # 4 sex
            gobject.TYPE_STRING)  # 5 points

        uifile = os.path.join(metarace.UI_PATH, u'classification.ui')
        _log.debug(u'Building event interface from %r', uifile)
        b = gtk.Builder()
        b.add_from_file(uifile)

        self.frame = b.get_object(u'classification_vbox')
        self.frame.connect(u'destroy', self.shutdown)

        # info pane
        self.info_expand = b.get_object(u'info_expand')
        b.get_object(u'classification_info_evno').set_text(self.evno)
        self.showev = b.get_object(u'classification_info_evno_show')
        self.prefix_ent = b.get_object(u'classification_info_prefix')
        self.prefix_ent.set_text(self.event[u'pref'])
        self.prefix_ent.connect(u'changed', self.editent_cb, u'pref')
        self.info_ent = b.get_object(u'classification_info_title')
        self.info_ent.set_text(self.event[u'info'])
        self.info_ent.connect(u'changed', self.editent_cb, u'info')

        self.context_menu = None
        if ui:
            # riders pane
            t = gtk.TreeView(self.riders)
            self.view = t
            t.set_rules_hint(True)

            # riders columns
            uiutil.mkviewcoltxt(t, u'No.', COL_BIB, calign=1.0)
            uiutil.mkviewcoltxt(t, u'Name', COL_NAME, expand=True)
            uiutil.mkviewcoltxt(t, u'Club', COL_CLUB)
            uiutil.mkviewcoltxt(t, u'Cat', COL_CAT)
            uiutil.mkviewcoltxt(t, u'Sex', COL_SEX)
            uiutil.mkviewcoltxt(t, u'Points', COL_POINTS, calign=1.0)
            t.show()
            b.get_object(u'classification_result_win').add(t)
            b.connect_signals(self)
