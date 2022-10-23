"""Classification/Medal meta-event handler for trackmeet."""

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

LOG = logging.getLogger(u'metarace.classification')
LOG.setLevel(logging.DEBUG)

# config version string
EVENT_ID = u'classification-2.0'

# Model columns
COL_BIB = 0
COL_FIRST = 1
COL_LAST = 2
COL_CLUB = 3
COL_COMMENT = 4
COL_PLACE = 5
COL_MEDAL = 6

# scb function key mappings
key_reannounce = u'F4'  # (+CTRL)
key_abort = u'F5'  # (+CTRL)
key_startlist = u'F3'
key_results = u'F4'


class classification(object):

    def loadconfig(self):
        """Load race config from disk."""
        cr = jsonconfig.config({
            u'event': {
                u'id': EVENT_ID,
                u'showinfo': True,
                u'showevents': u'',
                u'comments': [],
                u'placesrc': u'',
                u'medals': u''
            }
        })
        cr.add_section('event')
        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                LOG.error(u'Unable to read config: %s', e)
        else:
            LOG.info(u'%r not found, loading defaults', self.configfile)

        self.update_expander_lbl_cb()
        self.info_expand.set_expanded(
            strops.confopt_bool(cr.get(u'event', u'showinfo')))

        self.showevents = cr.get(u'event', u'showevents')
        self.placesrc = cr.get(u'event', u'placesrc')
        self.medals = cr.get(u'event', u'medals')
        self.comments = cr.get(u'event', u'comments')
        self.recalculate()  # model is cleared and loaded in recalc
        eid = cr.get(u'event', u'id')
        if eid and eid != EVENT_ID:
            LOG.info(u'Event config mismatch: %r != %r', eid, EVENT_ID)

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
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
        sec.lines = []
        for r in self.riders:
            rno = r[COL_BIB].decode(u'utf-8')
            if u't' in self.series:  # Team no hack
                rno = u' '  # force name
            rh = self.meet.newgetrider(rno, self.series)
            rname = u''
            if rh is not None:
                rname = rh[u'namestr']
            sec.lines.append([None, rno, rname, None, None, None])
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
            LOG.error(u'Attempt to save readonly event')
            return
        cw = jsonconfig.config()
        cw.add_section(u'event')
        cw.set(u'event', u'showevents', self.showevents)
        cw.set(u'event', u'placesrc', self.placesrc)
        cw.set(u'event', u'medals', self.medals)
        cw.set(u'event', u'comments', self.comments)
        cw.set(u'event', u'showinfo', self.info_expand.get_expanded())
        cw.set(u'event', u'id', EVENT_ID)
        LOG.debug(u'Saving event config %r', self.configfile)
        with metarace.savefile(self.configfile) as f:
            cw.write(f)

    def result_gen(self):
        """Generator function to export a final result."""
        for r in self.riders:
            bib = r[COL_BIB].decode(u'utf-8')
            rank = None
            info = u''
            rks = r[COL_PLACE].decode(u'utf-8')
            if rks:
                if rks.isdigit():
                    rank = int(rks)
                    info = r[COL_MEDAL].decode(u'utf-8')
                else:
                    # TODO: allow for 'dnf'/'dns' here, propagates into event
                    rank = rks
                    info = None  # no seeding info available
            time = None

            yield [bib, rank, time, info]

    def result_report(self, recurse=True):  # by default include inners
        """Return a list of report sections containing the race result."""
        ret = []

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
        sec.lines = []
        for r in self.riders:
            rno = r[COL_BIB].decode(u'utf-8')
            rh = self.meet.newgetrider(rno, self.series)
            rname = u''
            plink = u''
            rcat = u''
            if u't' in self.series:  # Team no hack
                rno = u' '  # force name
                if rh is not None:
                    rname = rh[u'first']
            else:
                if rh is not None:
                    rname = rh[u'namestr']
                    if rh[u'ucicode']:
                        rcat = rh[u'ucicode']  # overwrite by force

                    # consider partners here
                    if rh[u'cat'] and u'tandem' in rh[u'cat'].lower():
                        ph = self.meet.newgetrider(rh[u'note'], self.series)
                        if ph is not None:
                            plink = [
                                u'', u'', ph[u'namestr'] + u' - Pilot',
                                ph[u'ucicode'], u'', u'', u''
                            ]

            rank = u''
            rks = r[COL_PLACE].decode(u'utf-8')
            if rks:
                rank = rks
                if rank.isdigit():
                    rank += u'.'

            medal = u''
            mds = r[COL_MEDAL].decode(u'utf-8')
            if mds:
                medal = mds
            if medal == u'' and prevmedal != u'':
                # add empty line
                sec.lines.append([None, None, None])
            prevmedal = medal

            nrow = [rank, rno, rname, rcat, None, medal, plink]
            sec.lines.append(nrow)
            if u't' in self.series:
                for trno in strops.reformat_riderlist(rh[u'note']).split():
                    trh = self.meet.newgetrider(trno)  #!! SERIES?
                    if trh is not None:
                        trname = trh[u'namestr']
                        trinf = trh[u'ucicode']
                        sec.lines.append(
                            [None, trno, trname, trinf, None, None, None])
        ret.append(sec)

        if recurse:
            # then append each of the specified events
            for evno in self.showevents.split():
                if evno:
                    LOG.debug(u'Including results from event %r', evno)
                    r = self.meet.get_event(evno, False)
                    if r is None:
                        LOG.error(u'Invalid event %r in showplaces', evno)
                        continue
                    r.loadconfig()  # now have queryable event handle
                    if r.onestart:  # go for result
                        ret.extend(r.result_report())
                    else:  # go for startlist
                        ret.extend(r.startlist_report())
                    r.destroy()
        return ret

    def addrider(self, bib=u'', place=u''):
        """Add specified rider to race model."""
        nr = [bib, u'', u'', u'', u'', u'', u'']
        er = self.getrider(bib)
        if not bib or er is None:
            dbr = self.meet.rdb.getrider(bib, self.series)
            if dbr is not None:
                for i in range(1, 5):
                    nr[i] = self.meet.rdb.getvalue(dbr, i)  # unicode
            nr[COL_PLACE] = place
            return self.riders.append(nr)
        else:
            LOG.warning(u'Rider %r already in model', bib)
            return None

    def getrider(self, bib):
        """Return temporary reference to model row."""
        ret = None
        for r in self.riders:
            if r[COL_BIB].decode(u'utf-8') == bib:
                ret = r
                break
        return ret

    def delrider(self, bib):
        """Remove the specified rider from the model."""
        i = self.getiter(bib)
        if i is not None:
            self.riders.remove(i)

    def getiter(self, bib):
        """Return temporary iterator to model row."""
        i = self.riders.get_iter_first()
        while i is not None:
            if self.riders.get_value(i, COL_BIB).decode(u'utf-8') == bib:
                break
            i = self.riders.iter_next(i)
        return i

    def recalculate(self):
        """Update internal model."""

        # TODO: update to allow for keirin and sprint inter rounds
        self.riders.clear()

        # Pass one: Create ordered place lookup
        currank = 0
        lookup = {}
        for p in self.placesrc.split(u';'):
            placegroup = p.strip()
            if placegroup:
                LOG.debug(u'Adding place group %r at rank %r', placegroup,
                          currank)
                if placegroup == u'X':
                    LOG.debug(u'Added placeholder at rank %r', currank)
                    currank += 1
                else:
                    specvec = placegroup.split(u':')
                    if len(specvec) == 2:
                        evno = specvec[0].strip()
                        if evno not in lookup:
                            lookup[evno] = {}
                        if evno != self.evno:
                            placeset = strops.placeset(specvec[1])
                            for i in placeset:
                                lookup[evno][i] = currank
                                currank += 1
                        else:
                            LOG.warning(u'Ignored ref to self %r at rank %r',
                                        placegroup, currank)
                    else:
                        LOG.warning(u'Invalid placegroup %r at rank %r',
                                    placegroup, currank)
            else:
                LOG.debug(u'Empty placegroup at rank %r', currank)

        # Pass 2: create an ordered list of rider numbers using lookup
        placemap = {}
        maxcrank = 0
        for evno in lookup:
            r = self.meet.get_event(evno, False)
            if r is None:
                LOG.warning(u'Event %r not found for lookup %r', evno,
                            lookup[evno])
                return
            r.loadconfig()  # now have queryable event handle
            for res in r.result_gen():
                if isinstance(res[1], int):
                    if res[1] in lookup[evno]:
                        crank = lookup[evno][res[1]] + 1
                        maxcrank = max(maxcrank, crank)
                        LOG.debug(u'Assigned place %r to rider %r at rank %r',
                                  crank, res[0], res[1])
                        if crank not in placemap:
                            placemap[crank] = []
                        placemap[crank].append(res[0])

        # Pass 3: add riders to model in rank order
        i = 1
        while i <= maxcrank:
            if i in placemap:
                for r in placemap[i]:
                    self.addrider(r, unicode(i))
            i += 1

        if len(self.riders) > 0:  # got at least one result to report
            self.onestart = True
        # Pass 4: Mark medals if required
        medalmap = {}
        mcount = 1
        for m in self.medals.split():
            medalmap[mcount] = m
            mcount += 1
        for r in self.riders:
            rks = r[COL_PLACE].decode(u'utf-8')
            if rks.isdigit():
                rank = int(rks)
                if rank in medalmap:
                    r[COL_MEDAL] = medalmap[rank]
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
        return self.do_places()

    def do_places(self):
        """Show race result on scoreboard."""
        # Draw a 'medal ceremony' on the screen
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
        LOG.debug('Shutdown event %s: %s', self.evno, msg)
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
        prfile = os.path.join(metarace.UI_PATH,
                              u'classification_properties.ui')
        LOG.debug(u'Building event properties from %r', prfile)
        b = gtk.Builder()
        b.add_from_file(prfile)
        dlg = b.get_object(u'properties')
        dlg.set_transient_for(self.meet.window)
        se = b.get_object(u'race_series_entry')
        se.set_text(self.series)
        ee = b.get_object(u'race_showevents_entry')
        ee.set_text(self.showevents)
        pe = b.get_object(u'race_placesrc_entry')
        pe.set_text(self.placesrc)
        me = b.get_object(u'race_medals_entry')
        me.set_text(self.medals)
        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            LOG.debug(u'Updating event properties')
            self.placesrc = pe.get_text().decode(u'utf-8')
            self.medals = me.get_text().decode(u'utf-8')
            self.showevents = ee.get_text().decode(u'utf-8')

            # update series
            ns = se.get_text().decode(u'utf-8')
            if ns != self.series:
                self.series = ns
                self.event[u'seri'] = ns

            self.recalculate()
            glib.idle_add(self.delayed_announce)
        else:
            LOG.debug(u'Edit event properties cancelled')

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
        LOG.debug(u'Init event %s', self.evno)

        # race run time attributes
        self.onestart = True  # always true for autospec classification
        self.readonly = not ui
        self.winopen = ui
        self.placesrc = u''
        self.medals = u''
        self.comments = []

        self.riders = gtk.ListStore(
            gobject.TYPE_STRING,  # 0 bib
            gobject.TYPE_STRING,  # 1 first name
            gobject.TYPE_STRING,  # 2 last name
            gobject.TYPE_STRING,  # 3 club
            gobject.TYPE_STRING,  # 4 comment
            gobject.TYPE_STRING,  # 5 place
            gobject.TYPE_STRING)  # 6 medal

        uifile = os.path.join(metarace.UI_PATH, u'classification.ui')
        LOG.debug(u'Building event interface from %r', uifile)
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
            uiutil.mkviewcoltxt(t,
                                u'First Name',
                                COL_FIRST,
                                self.editcol_db,
                                expand=True)
            uiutil.mkviewcoltxt(t,
                                u'Last Name',
                                COL_LAST,
                                self.editcol_db,
                                expand=True)
            uiutil.mkviewcoltxt(t, u'Club', COL_CLUB, self.editcol_db)
            uiutil.mkviewcoltxt(t, u'Rank', COL_PLACE, halign=0.5, calign=0.5)
            uiutil.mkviewcoltxt(t, u'Medal', COL_MEDAL)
            t.show()
            b.get_object(u'classification_result_win').add(t)
            b.connect_signals(self)
