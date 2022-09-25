# SPDX-License-Identifier: MIT
"""Timing and data handling application wrapper for road events."""

import pygtk

pygtk.require("2.0")

import gtk
import glib
import gobject

import os
import sys
import logging
import random

import metarace

from metarace import ucsv
from metarace import jsonconfig
from metarace import tod
from metarace import riderdb
from metarace import eventdb
from metarace import telegraph
from metarace import export
from metarace import decoder
from metarace import timy
from metarace import strops
from metarace import loghandler
from metarace import namebank
from metarace import report
from metarace import uiutil
from metarace import irtt
from metarace import trtt
from metarace import rms

LOGFILE = u'event.log'
LOGFILE_LEVEL = logging.DEBUG
CONFIGFILE = u'config.json'
ROADMEET_ID = u'roadmeet_3.0'  # configuration versioning
EXPORTPATH = u'export'
LOG = logging.getLogger(u'metarace.roadmeet')
LOG.setLevel(logging.DEBUG)
ROADRACE_TYPES = {
    u'road': u'Road Race',
    u'circuit': u'Circuit',
    u'criterium': u'Criterium',
    u'handicap': u'Handicap',
    u'cross': u'Cyclocross',
    u'irtt': u'Road Time Trial',
    u'trtt': u'Team Road Time Trial',
}


class registerdlg(object):

    def __init__(self, meet=None):
        self.rdb = meet.rdb
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'transponder_reg.ui'))
        self.dlg = b.get_object(u'tagregister')
        self.dlg.set_transient_for(meet.window)
        self.rfidval = b.get_object(u'rfid_val')
        self.bibent = b.get_object(u'bib_entry')
        self.serent = b.get_object(u'series_entry')
        self.riderval = b.get_object(u'rider_val')
        self.autoinc = b.get_object(u'autoinc_but')
        self.autotrack = b.get_object(u'autotrack_but')
        self.storeid = None  # TODO: track scanned rfid with changing bib
        b.connect_signals(self)

    def series_entry_changed_cb(self, entry, data=None):
        bib = self.bibent.get_text().decode(u'utf-8', u'replace')
        if bib:
            self.bib_entry_changed_cb(self.bibent)

    def bib_entry_changed_cb(self, entry, data=None):
        bib = entry.get_text().decode(u'utf-8', u'replace')
        ser = self.serent.get_text().decode(u'utf-8', u'replace')
        r = self.rdb.getrider(bib, ser)
        if r is not None:
            first = self.rdb.getvalue(r, riderdb.COL_FIRST)
            last = self.rdb.getvalue(r, riderdb.COL_LAST)
            club = self.rdb.getvalue(r, riderdb.COL_CLUB)
            cat = self.rdb.getvalue(r, riderdb.COL_CAT)
            refid = self.rdb.getvalue(r, riderdb.COL_REFID)
            self.riderval.set_text(u'{0} {1} ({2}) / {3}'.format(
                first, last, club, cat))
            if refid:
                self.rfidval.set_text(refid)
            else:
                self.rfidval.set_text(u'')
        else:
            self.riderval.set_text(u'[new rider]')
            self.rfidval.set_text(u'')

    def rfid_val_activate_cb(self, entry, data=None):
        """Activate on rfid val updates rider record."""
        bib = self.bibent.get_text().decode(u'utf-8', u'replace')
        ser = self.serent.get_text().decode(u'utf-8', u'replace')
        nrid = self.rfidval.get_text().decode(u'utf-8', u'replace')
        r = self.rdb.getrider(bib, ser)
        if r is not None:
            self.rdb.editrider(r, refid=nrid)
            LOG.info(u'Updated transponder id for rider %r to %r', bib, nrid)

    def bib_entry_activate_cb(self, entry, data=None):
        """Activate on bib adds new rider, unless it exists."""
        bib = entry.get_text().decode(u'utf-8', u'replace')
        ser = self.serent.get_text().decode(u'utf-8', u'replace')
        r = self.rdb.getrider(bib, ser)
        if r is None:
            self.rdb.addempty(bib, ser)
            LOG.debug(u'Added new rider %r:%r', bib, ser)

    def run(self):
        return self.dlg.run()

    def destroy(self):
        self.dlg.destroy()

    def increment_rider(self):
        cbib = self.bibent.get_text().decode(u'utf-8', u'replace').strip()
        cser = self.serent.get_text().decode(u'utf-8', u'replace').strip()
        LOG.debug(u'Increment rider from %r:%r', cbib, cser)
        if cbib.isdigit():
            if self.autotrack.get_active():
                nbib = self.rdb.nextriderin(cbib, cser)
                LOG.debug(u'Next rider from DB: %r:%r', nbib, cser)
                if nbib is not None:
                    self.bibent.set_text(nbib)
                    self.bibent.activate()
            else:
                nbib = int(cbib) + 1
                LOG.debug(u'Next rider number: %r:%r', nbib, cser)
                self.bibent.set_text(unicode(nbib))
                self.bibent.activate()
        return False

    def register_tag(self, e):
        if e.refid:
            r = self.rdb.getrefid(e.refid)
            if r is not None:
                # transponder aleady assigned to a rider, load them
                bib = self.rdb.getvalue(r, riderdb.COL_BIB)
                ser = self.rdb.getvalue(r, riderdb.COL_SERIES)
                self.rfidval.set_text(e.refid)
                self.bibent.set_text(bib)
                self.serent.set_text(ser)
            else:
                # transponder currently unassigned
                bib = self.bibent.get_text().decode(u'utf-8', u'replace')
                if bib:
                    ser = self.serent.get_text().decode(u'utf-8', u'replace')
                    self.bibent.activate()  # required?
                    r = self.rdb.getrider(bib, ser)
                    if r is not None:
                        # check for existing tag allocation
                        orefid = self.rdb.getvalue(r, riderdb.COL_REFID)
                        if not orefid:
                            nrefid = e.refid.lower()
                            self.rdb.editrider(r, refid=nrefid)
                            LOG.warning(u'Assigned ID %r to %r:%r', nrefid,
                                        bib, ser)
                            self.rfidval.set_text(nrefid)
                            if self.autoinc.get_active():
                                glib.idle_add(self.increment_rider)
                            else:
                                self.bibent.grab_focus()
                        else:
                            LOG.warning(u'Rider %r:%r already assigned to %r',
                                        bib, ser, orefid)
                else:
                    LOG.warning(u'Rider number entry empty')
                    self.rfidval.set_text(e.refid.lower())


class fakemeet(object):
    """Road meet placeholder for external event manipulations."""

    def __init__(self, edb, rdb):
        self.edb = edb
        self.rdb = rdb
        self.timer = decoder.decoder()
        self.alttimer = timy.timy()
        self.stat_but = uiutil.statbut(gtk.Button())
        self.action_model = gtk.ListStore(
            gobject.TYPE_STRING,
            gobject.TYPE_STRING,
        )
        self.action_model.append(['a', 'a'])
        self.action_combo = gtk.ComboBox()
        self.action_combo.set_model(self.action_model)
        self.action_combo.set_active(0)

        self.announce = telegraph.telegraph()
        self.title_str = ''
        self.subtitle_str = ''
        self.date_str = ''
        self.organiser_str = ''
        self.commissaire_str = ''
        self.distance = None
        self.docindex = 0
        self.linkbase = u'.'
        self.provisionalstart = False
        self.indexlink = None
        self.nextlink = None
        self.prevlink = None
        self.bibs_in_results = True

    def get_distance(self):
        return self.distance

    def cmd_announce(self, command, msg):
        return False

    def rider_announce(self, rvec):
        return False

    def timer_announce(self, evt, timer=None, source=u''):
        return False

    def report_strings(self, rep):
        """Copy the meet strings into the supplied report."""

        ## this is a copy of meet.print_report()
        rep.strings[u'title'] = self.title_str
        rep.strings[u'subtitle'] = self.subtitle_str
        rep.strings[u'docstr'] = self.document_str
        rep.strings[u'datestr'] = strops.promptstr(u'Date:', self.date_str)
        rep.strings[u'commstr'] = strops.promptstr(u'Chief Commissaire:',
                                                   self.commissaire_str)
        rep.strings[u'orgstr'] = strops.promptstr(u'Organiser:',
                                                  self.organiser_str)
        if self.distance:
            rep.strings[u'diststr'] = strops.promptstr(
                u'Distance:',
                unicode(self.distance) + u'\u2006km')
        else:
            rep.strings[u'diststr'] = self.diststr

        if self.prevlink:
            rep.prevlink = self.prevlink
        if self.nextlink:
            rep.nextlink = self.nextlink
        if self.indexlink:
            rep.indexlink = self.indexlink
        if self.shortname:
            rep.shortname = self.shortname

    def loadconfig(self):
        """Load meet config from disk."""
        cr = jsonconfig.config({
            u'roadmeet': {
                u'title': u'',
                u'shortname': u'',
                u'subtitle': u'',
                u'document': u'',
                u'date': u'',
                u'organiser': u'',
                u'commissaire': u'',
                u'distance': None,
                u'diststr': u'',
                u'docindex': u'0',
                u'linkbase': u'.',
                u'indexlink': None,
                u'nextlink': None,
                u'prevlink': None,
                u'resultnos': u'Yes',
                u'provisionalstart': False,
                u'competitioncode': u'',
                u'eventcode': u'',
                u'racetype': u'',
                u'competitortype': u'',
                u'documentversion': u'',
                u'id': u''
            }
        })
        cr.add_section(u'roadmeet')
        cr.merge(metarace.sysconf, u'roadmeet')
        # check for config file
        try:
            with open(CONFIGFILE, 'rb') as f:
                cr.read(f)
        except Exception as e:
            LOG.error(u'Error reading meet config: %s', e)
        # set meet meta, and then copy into text entries
        self.shortname = cr.get(u'roadmeet', u'shortname')
        self.title_str = cr.get(u'roadmeet', u'title')
        self.subtitle_str = cr.get(u'roadmeet', u'subtitle')
        self.document_str = cr.get(u'roadmeet', u'document')
        self.date_str = cr.get(u'roadmeet', u'date')
        self.organiser_str = cr.get(u'roadmeet', u'organiser')
        self.commissaire_str = cr.get(u'roadmeet', u'commissaire')
        self.linkbase = cr.get(u'roadmeet', u'linkbase')
        self.distance = cr.get_float(u'roadmeet', u'distance')
        self.diststr = cr.get(u'roadmeet', u'diststr')
        self.docindex = cr.get_posint(u'roadmeet', u'docindex', 0)
        self.competitioncode = cr.get(u'roadmeet', u'competitioncode')
        self.eventcode = cr.get(u'roadmeet', u'eventcode')
        self.racetype = cr.get(u'roadmeet', u'racetype')
        self.linkbase = cr.get(u'roadmeet', u'linkbase')
        self.indexlink = cr.get(u'roadmeet', u'indexlink')
        self.prevlink = cr.get(u'roadmeet', u'prevlink')
        self.nextlink = cr.get(u'roadmeet', u'nextlink')
        self.competitortype = cr.get(u'roadmeet', u'competitortype')
        self.documentversion = cr.get(u'roadmeet', u'documentversion')
        self.bibs_in_results = cr.get_bool(u'roadmeet', u'resultnos')
        self.provisionalstart = cr.get_bool(u'roadmeet', u'provisionalstart')

    def event_configfile(self, evno):
        """Return a config filename for the given event no."""
        return u'event_{}.json'.format(unicode(evno))


class roadmeet(object):
    """Road meet application class."""

    ## Meet Menu Callbacks
    def menu_meet_save_cb(self, menuitem, data=None):
        """Save current all meet data to config."""
        self.saveconfig()

    def get_short_name(self):
        """Return the <= 16 char shortname."""
        return self.shortname

    def cat_but_auto_clicked(self, but, entry, data=None):
        """Lookup cats and write them into the supplied entry."""
        entry.set_text(u' '.join(self.rdb.listcats()))

    def menu_race_properties_activate_cb(self, menuitem, data=None):
        """Edit race specific properties."""
        if self.curevent is not None:
            LOG.debug(u'Editing race properties')
            self.curevent.edit_event_properties(self.window)

    def menu_meet_properties_cb(self, menuitem, data=None):
        """Edit meet properties."""
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'roadmeet_props.ui'))
        dlg = b.get_object(u'properties')
        dlg.set_transient_for(self.window)

        # setup the type entry
        tcombo = b.get_object(u'type_combo')
        tmodel = b.get_object(u'type_model')
        tlbl = self.etype
        dotype = False
        # correct empty type
        if self.etype == u'':
            self.etype = u'road'
        if self.etype in ROADRACE_TYPES:
            tlbl = ROADRACE_TYPES[self.etype]
            dotype = True
            cnt = 0
            for t in [
                    u'road', u'circuit', u'handicap', u'criterium', u'cross',
                    u'irtt', u'trtt'
            ]:
                tmodel.append([t, ROADRACE_TYPES[t]])
                if t == self.etype:
                    tcombo.set_active(cnt)
                cnt += 1
            tcombo.set_sensitive(True)
        else:
            LOG.warning(u'Unknown event type %r', self.etype)
            tmodel.append([self.etype, tlbl])
            tcombo.set_active(0)
            tcombo.set_sensitive(False)

        # fetch event result categories
        ocats = []
        cat_ent = b.get_object(u'cat_entry')
        if self.curevent is not None:
            ocats = self.curevent.get_catlist()
            cat_ent.set_text(u' '.join(ocats))
            cba = b.get_object(u'cat_but_auto')
            cba.connect(u'clicked', self.cat_but_auto_clicked, cat_ent)

        # fill text entries
        t_ent = b.get_object(u'title_entry')
        t_ent.set_text(self.title_str)
        st_ent = b.get_object(u'subtitle_entry')
        st_ent.set_text(self.subtitle_str)
        doc_ent = b.get_object(u'document_entry')
        doc_ent.set_text(self.document_str)
        d_ent = b.get_object(u'date_entry')
        d_ent.set_text(self.date_str)
        o_ent = b.get_object(u'organiser_entry')
        o_ent.set_text(self.organiser_str)
        c_ent = b.get_object(u'commissaire_entry')
        c_ent.set_text(self.commissaire_str)
        di_ent = b.get_object(u'distance_entry')
        if self.distance is not None:
            di_ent.set_text(str(self.distance))
        dis_ent = b.get_object(u'diststr_entry')
        dis_ent.set_text(self.diststr)
        ate = b.get_object(u'announce_topic_entry')
        if self.anntopic is not None:
            ate.set_text(self.anntopic)
        tte = b.get_object(u'timing_topic_entry')
        if self.timertopic is not None:
            tte.set_text(self.timertopic)
        ren = b.get_object(u'remote_enable_check')
        ren.set_active(self.remote_enable)
        mte = b.get_object(u'timing_main_entry')
        mte.set_text(self.timer_port)
        alte = b.get_object('timing_alt_entry')
        alte.set_text(self.alttimer_port)
        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            LOG.debug(u'Updating meet properties')
            self.title_str = t_ent.get_text().decode(u'utf-8')
            self.subtitle_str = st_ent.get_text().decode(u'utf-8')
            self.document_str = doc_ent.get_text().decode(u'utf-8')
            self.date_str = d_ent.get_text().decode(u'utf-8')
            self.organiser_str = o_ent.get_text().decode(u'utf-8')
            self.commissaire_str = c_ent.get_text().decode(u'utf-8')
            self.distance = strops.confopt_float(
                di_ent.get_text().decode(u'utf-8'))
            self.diststr = dis_ent.get_text().decode(u'utf-8')

            # 'announce' topic
            ntopic = ate.get_text().decode(u'utf-8')
            if ntopic != self.anntopic:
                if self.anntopic is not None:
                    self.announce.unsubscribe(u'/'.join(
                        (self.anntopic, u'control', u'#')))
                self.anntopic = None
                if ntopic:
                    self.anntopic = ntopic
                    self.announce.subscribe(u'/'.join(
                        (self.anntopic, u'control', u'#')))
            # remote timer topic
            ntopic = tte.get_text().decode(u'utf-8')
            if ntopic != self.timertopic:
                if self.timertopic is not None:
                    self.announce.unsubscribe(self.timertopic)
                self.timertopic = None
                if ntopic:
                    self.timertopic = ntopic

            # update remote subscription
            self.remote_enable = ren.get_active()
            self.remote_reset()

            # reset timer
            self.set_timer(mte.get_text())

            nport = alte.get_text().decode(u'utf-8')
            if nport != self.alttimer_port:
                self.alttimer_port = nport
                self.alttimer.setport(nport)

            reload = False
            if self.curevent is not None:
                ncats = cat_ent.get_text().decode(u'utf-8').split()
                if ncats != ocats:
                    LOG.debug(u'Result cats changed %r -> %r', ocats, ncats)
                    self.curevent.loadcats(ncats)
                    reload = True
            nt = tmodel.get_value(tcombo.get_active_iter(), 0).decode(u'utf-8')
            if dotype:
                # check for type change
                if nt != self.etype:
                    LOG.info(u'Event type changed from %r to %r', self.etype,
                             nt)
                    if nt == u'crit':
                        self.curevent.downtimes(False)
                    else:
                        self.curevent.downtimes(True)
                    reload = True
            if reload:
                event = self.edb.getfirst()
                event[u'type'] = nt
                self.etype = nt
                self.menu_race_run_activate_cb(None, None)

            self.set_title()
            LOG.debug(u'Properties updated')
        else:
            LOG.debug(u'Edit properties cancelled')
        dlg.destroy()

    def print_report(self, sections=[], provisional=False):
        """Print the pre-formatted sections in a standard report."""
        rep = report.report()
        rep.provisional = provisional
        rep.strings[u'title'] = self.title_str
        rep.strings[u'subtitle'] = self.subtitle_str
        rep.strings[u'docstr'] = self.document_str
        rep.strings[u'datestr'] = strops.promptstr(u'Date:', self.date_str)
        rep.strings[u'commstr'] = strops.promptstr(u'Chief Commissaire:',
                                                   self.commissaire_str)
        rep.strings[u'orgstr'] = strops.promptstr(u'Organiser:',
                                                  self.organiser_str)
        if self.distance:
            rep.strings[u'diststr'] = strops.promptstr(
                u'Distance:',
                unicode(self.distance) + u'\u2006km')
        else:
            rep.strings[u'diststr'] = self.diststr

        if self.prevlink:
            rep.prevlink = self.prevlink
        if self.nextlink:
            rep.nextlink = self.nextlink
        if self.indexlink:
            rep.indexlink = self.indexlink
        if self.shortname:
            rep.shortname = self.shortname
        for sec in sections:
            rep.add_section(sec)

        print_op = gtk.PrintOperation()
        print_op.set_allow_async(True)
        print_op.set_print_settings(self.printprefs)
        print_op.set_default_page_setup(self.pageset)
        print_op.connect(u'begin_print', self.begin_print, rep)
        print_op.connect(u'draw_page', self.draw_print_page, rep)
        res = print_op.run(gtk.PRINT_OPERATION_ACTION_PREVIEW, None)
        if res == gtk.PRINT_OPERATION_RESULT_APPLY:
            self.printprefs = print_op.get_print_settings()
            LOG.debug(u'Updated print preferences')
        elif res == gtk.PRINT_OPERATION_RESULT_IN_PROGRESS:
            LOG.debug(u'Print operation in progress')
        self.docindex += 1

        # For convenience, also save copies to pdf and xls
        ofile = u'output.pdf'
        with metarace.savefile(ofile) as f:
            rep.output_pdf(f)
        ofile = u'output.xls'
        with metarace.savefile(ofile) as f:
            rep.output_xls(f)
        return False

    def begin_print(self, operation, context, rep):
        """Set print pages and units."""
        rep.start_gtkprint(context.get_cairo_context())
        operation.set_use_full_page(True)
        operation.set_n_pages(rep.get_pages())
        operation.set_unit('points')

    def draw_print_page(self, operation, context, page_nr, rep):
        """Draw to the nominated page."""
        rep.set_context(context.get_cairo_context())
        rep.draw_page(page_nr)

    def menu_meet_quit_cb(self, menuitem, data=None):
        """Quit the application."""
        self.running = False
        self.window.destroy()

    ## Race Menu Callbacks
    def menu_race_run_activate_cb(self, menuitem=None, data=None):
        """Open the event handler."""
        eh = self.edb.getfirst()
        if eh is not None:
            self.open_event(eh)
            self.set_title()

    def menu_race_close_activate_cb(self, menuitem, data=None):
        """Close callback - disabled in roadrace."""
        self.close_event()

    def menu_race_abort_activate_cb(self, menuitem, data=None):
        """Close the currently open event without saving."""
        if self.curevent is not None:
            self.curevent.readonly = True
        self.close_event()

    def menu_race_armstart_activate_cb(self, menuitem, data=None):
        """Default armstart handler."""
        LOG.info(u'Arm Start')
        try:
            self.curevent.armstart()
        except Exception as e:
            LOG.error(u'Arm start %s: %s', e.__class__.__name__, e)

    def menu_race_armlap_activate_cb(self, menuitem, data=None):
        """Default armlap handler."""
        LOG.debug(u'Arm Lap')
        try:
            self.curevent.armlap()
        except Exception as e:
            LOG.error(u'Arm lap %s: %s', e.__class__.__name__, e)

    def menu_race_armfin_activate_cb(self, menuitem, data=None):
        """Default armfin handler."""
        LOG.info(u'Arm Finish')
        try:
            self.curevent.armfinish()
        except Exception as e:
            LOG.error(u'Arm finish %s: %s', e.__class__.__name__, e)

    def menu_race_finished_activate_cb(self, menuitem, data=None):
        """Default finished handler."""
        LOG.info(u'Finished')
        try:
            self.curevent.set_finished()
        except Exception as e:
            LOG.error(u'Set finished %s: %s', e.__class__.__name__, e)

    def open_event(self, eventhdl=None):
        """Open provided event handle."""
        if eventhdl is not None:
            self.close_event()
            if self.etype not in ROADRACE_TYPES:
                LOG.warning(u'Unknown event type %r', self.etype)
            if self.etype == u'irtt':
                self.curevent = irtt.irtt(self, eventhdl, True)
            elif self.etype == u'trtt':
                self.curevent = trtt.trtt(self, eventhdl, True)
            else:
                self.curevent = rms.rms(self, eventhdl, True)

            self.curevent.loadconfig()
            self.race_box.add(self.curevent.frame)

            # re-populate the rider command model.
            cmdo = self.curevent.get_ridercmdorder()
            cmds = self.curevent.get_ridercmds()
            if cmds is not None:
                self.action_model.clear()
                for cmd in cmdo:
                    self.action_model.append([cmd, cmds[cmd]])
                self.action_combo.set_active(0)

            self.menu_race_close.set_sensitive(True)
            self.menu_race_abort.set_sensitive(True)
            starters = eventhdl[u'star']
            if starters is not None and starters != u'':
                self.curevent.race_ctrl(u'add', starters)
                eventhdl[u'star'] = u''  # and clear
            self.curevent.show()

    def close_event(self):
        """Close the currently opened race."""
        if self.curevent is not None:
            self.curevent.hide()
            self.race_box.remove(self.curevent.frame)
            self.curevent.destroy()
            self.menu_race_close.set_sensitive(False)
            self.menu_race_abort.set_sensitive(False)
            self.curevent = None
            self.stat_but.buttonchg(uiutil.bg_none, u'Closed')
            self.stat_but.set_sensitive(False)

    ## Reports menu callbacks.
    def menu_reports_startlist_activate_cb(self, menuitem, data=None):
        """Generate a startlist."""
        if self.curevent is not None:
            sections = self.curevent.startlist_report()
            if sections:
                self.print_report(sections)
            else:
                LOG.info(u'Startlist - Nothing to print')

    def menu_reports_callup_activate_cb(self, menuitem, data=None):
        """Generate a start line call-up."""
        if self.curevent is not None:
            sections = self.curevent.callup_report()
            if sections:
                self.print_report(sections)
            else:
                LOG.info(u'Callup - Nothing to print')

    def menu_reports_signon_activate_cb(self, menuitem, data=None):
        """Generate a sign on sheet."""
        if self.curevent is not None:
            sections = self.curevent.signon_report()
            if sections:
                self.print_report(sections)
            else:
                LOG.info(u'Sign-on - Nothing to print')

    def menu_reports_analysis_activate_cb(self, menuitem, data=None):
        """Generate the analysis report."""
        if self.curevent is not None:
            sections = self.curevent.analysis_report()
            if sections:
                self.print_report(sections)

    def menu_reports_camera_activate_cb(self, menuitem, data=None):
        """Generate the camera operator report."""
        if self.curevent is not None:
            sections = self.curevent.camera_report()
            if sections:
                self.print_report(sections)

    def race_results_points_activate_cb(self, menuitem, data=None):
        """Generate the points tally report."""
        if self.curevent is not None:
            sections = self.curevent.points_report()
            if sections:
                self.print_report(sections)

    def menu_reports_result_activate_cb(self, menuitem, data=None):
        """Generate the race result report."""
        if self.curevent is not None:
            sections = self.curevent.result_report()
            if sections:
                self.print_report(sections,
                                  self.curevent.timerstat != u'finished')

    def menu_data_rego_activate_cb(self, menuitem, data=None):
        """Open transponder registration dialog."""
        rdlg = registerdlg(self)
        ocb = self.timercb
        LOG.debug(u'Save ocb %r', ocb)
        try:
            self.timercb = rdlg.register_tag
            rdlg.run()
        finally:
            self.timercb = ocb
            LOG.debug(u'Restored cb %r', ocb)
        rdlg.destroy()

    def menu_data_uscb_activate_cb(self, menuitem, data=None):
        """Reload rider db from disk."""
        self.rdb.clear()
        self.rdb.load(u'riders.csv')
        LOG.info(u'Reloaded riders from disk')
        self.menu_race_run_activate_cb()

    def menu_import_riders_activate_cb(self, menuitem, data=None):
        """Add riders to database."""
        sfile = uiutil.loadcsvdlg(u'Select rider file to import', self.window,
                                  u'.')
        if sfile is not None:
            with namebank.namebank() as n:
                self.rdb.load(sfile, namedb=n, overwrite=True)
            LOG.info(u'Import riders from %r', sfile)
        else:
            LOG.debug(u'Import riders cancelled')

    def menu_import_chipfile_activate_cb(self, menuitem, data=None):
        """Import a transponder chipfile."""
        sfile = uiutil.loadcsvdlg(u'Select chipfile to import', self.window,
                                  u'.')
        if sfile is not None:
            self.rdb.load_chipfile(sfile)
            LOG.info(u'Import chipfile %r', sfile)
        else:
            LOG.debug(u'Import chipfile cancelled')

    def menu_import_startlist_activate_cb(self, menuitem, data=None):
        """Import a startlist."""
        if self.curevent is None:
            LOG.info(u'No event open for starters import')
            return
        count = 0
        sfile = uiutil.loadcsvdlg(u'Select startlist file to import',
                                  self.window, u'.')
        if os.path.isfile(sfile):
            with open(sfile, 'rb') as f:
                cr = ucsv.UnicodeReader(f)
                for r in cr:
                    if len(r) > 1 and r[1].isalnum() and r[1] != u'No.':
                        bib = r[1].strip().lower()
                        series = u''
                        if len(r) > 2:
                            series = r[2].strip()
                        self.curevent.addrider(bib, series)
                        start = tod.mktod(r[0])
                        if start is not None:
                            self.curevent.starttime(start, bib, series)
                        count += 1
            LOG.info(u'Import %r starters from %r', count, sfile)
        else:
            LOG.debug(u'Import startlist cancelled')

    def menu_export_riders_activate_cb(self, menuitem, data=None):
        """Export rider database."""
        sfile = uiutil.savecsvdlg(u'Select file to export riders to',
                                  self.window, u'riders_export.csv', u'.')
        if sfile is not None:
            self.rdb.save(sfile)
            LOG.info(u'Export rider data to %r', sfile)
        else:
            LOG.debug(u'Export rider data cancelled')

    def menu_export_chipfile_activate_cb(self, menuitem, data=None):
        """Export transponder chipfile from rider model."""
        sfile = uiutil.savecsvdlg(u'Select file to export refids to',
                                  self.window, u'chipfile.csv', u'.')
        if sfile is not None:
            self.rdb.save_chipfile(sfile)
            LOG.info(u'Export chipfile to %r', sfile)
        else:
            LOG.debug(u'Export chipfile cancelled')

    def menu_export_result_activate_cb(self, menuitem, data=None):
        """Export raw result to disk."""
        if self.curevent is None:
            LOG.info(u'No event open')
            return

        rfilename = uiutil.savecsvdlg(u'Select file to save results to.',
                                      self.window, u'results.csv', u'.')
        if rfilename is not None:
            with metarace.savefile(rfilename) as f:
                cw = ucsv.UnicodeWriter(f)
                cw.writerow([u'Rank', u'No.', u'Time', u'Bonus', u'Penalty'])
                for r in self.curevent.result_gen(u''):
                    opr = [u'', u'', u'', u'', u'']
                    for i in range(0, 2):
                        if r[i]:
                            opr[i] = unicode(r[i])
                    for i in range(2, 5):
                        if r[i]:
                            opr[i] = unicode(r[i].timeval)
                    cw.writerow(opr)
            LOG.info(u'Export result to %r', rfilename)

    def menu_export_startlist_activate_cb(self, menuitem, data=None):
        """Extract startlist from current event."""
        if self.curevent is None:
            LOG.info(u'No event open')
            return

        rfilename = uiutil.savecsvdlg(u'Select file to save startlist to.',
                                      self.window, u'startlist.csv', u'.')
        if rfilename is not None:
            with metarace.savefile(rfilename) as f:
                cw = ucsv.UnicodeWriter(f)
                cw.writerow([u'Start', u'No.', u'Series', u'Name', u'Cat'])
                clist = self.curevent.get_catlist()
                clist.append(u'')
                for c in clist:
                    for r in self.curevent.startlist_gen(c):
                        cw.writerow(r)
            LOG.info(u'Export startlist to %r', rfilename)

    def export_result_maker(self):
        if self.mirrorfile:
            filebase = self.mirrorfile
        else:
            filebase = u'.'
        if filebase in [u'', u'.']:
            filebase = u''
            LOG.warn(u'Using default filenames for export')
        else:
            pass

        fnv = []
        if filebase:
            fnv.append(filebase)
        fnv.append(u'startlist')
        sfile = u'_'.join(fnv)
        fnv[-1] = 'result'
        ffile = u'_'.join(fnv)

        # Write out a startlist if event idle
        if self.curevent.timerstat in ['idle']:
            filename = sfile
            rep = report.report()
            rep.strings[u'title'] = self.title_str
            rep.strings[u'subtitle'] = self.subtitle_str
            rep.strings[u'docstr'] = self.document_str
            rep.strings[u'datestr'] = strops.promptstr(u'Date:', self.date_str)
            rep.strings[u'commstr'] = strops.promptstr(u'Chief Commissaire:',
                                                       self.commissaire_str)
            rep.strings[u'orgstr'] = strops.promptstr(u'Organiser:',
                                                      self.organiser_str)
            if self.distance:
                rep.strings[u'diststr'] = strops.promptstr(
                    u'Distance:',
                    unicode(self.distance) + u'\u2006km')
            else:
                rep.strings[u'diststr'] = self.diststr
            if self.provisionalstart:
                rep.set_provisional(True)
            rep.indexlink = u'index'
            if self.prevlink:
                rep.prevlink = u'_'.join((self.prevlink, u'startlist'))
            if self.nextlink:
                rep.nextlink = u'_'.join((self.nextlink, u'startlist'))
            if self.indexlink:
                rep.indexlink = self.indexlink
            if self.shortname:
                rep.shortname = self.shortname
            rep.resultlink = ffile
            for sec in self.curevent.startlist_report():
                rep.add_section(sec)

            lb = os.path.join(self.linkbase, filename)
            lt = [u'pdf', u'xls']
            rep.canonical = u'.'.join([lb, u'json'])
            ofile = os.path.join(self.exportpath, filename + u'.pdf')
            with metarace.savefile(ofile) as f:
                rep.output_pdf(f)
            ofile = os.path.join(self.exportpath, filename + u'.xls')
            with metarace.savefile(ofile) as f:
                rep.output_xls(f)
            ofile = os.path.join(self.exportpath, filename + u'.json')
            with metarace.savefile(ofile) as f:
                rep.output_json(f)
            ofile = os.path.join(self.exportpath, filename + u'.html')
            with metarace.savefile(ofile) as f:
                rep.output_html(f, linkbase=lb, linktypes=lt)

        # Then export a result
        rep = report.report()
        rep.strings[u'title'] = self.title_str
        rep.strings[u'subtitle'] = self.subtitle_str
        rep.strings[u'docstr'] = self.document_str
        rep.strings[u'datestr'] = strops.promptstr(u'Date:', self.date_str)
        rep.strings[u'commstr'] = strops.promptstr(u'Chief Commissaire:',
                                                   self.commissaire_str)
        rep.strings[u'orgstr'] = strops.promptstr(u'Organiser:',
                                                  self.organiser_str)
        if self.distance:
            rep.strings[u'diststr'] = strops.promptstr(
                u'Distance:',
                unicode(self.distance) + u'\u2006km')
        else:
            rep.strings[u'diststr'] = self.diststr

        # Set provisional status	# TODO: other tests for prov flag?
        if self.curevent.timerstat != u'finished':
            rep.set_provisional(True)
        else:
            rep.reportstatus = u'final'  # TODO: write in other phases
        for sec in self.curevent.result_report():
            rep.add_section(sec)

        filename = ffile
        rep.indexlink = u'index'
        if self.prevlink:
            rep.prevlink = u'_'.join((self.prevlink, u'result'))
        if self.nextlink:
            rep.nextlink = u'_'.join((self.nextlink, u'result'))
        if self.indexlink:
            rep.indexlink = self.indexlink
        if self.shortname:
            rep.shortname = self.shortname
        rep.startlink = sfile
        lb = os.path.join(self.linkbase, filename)
        lt = [u'pdf', u'xls']
        rep.canonical = u'.'.join([lb, u'json'])

        ofile = os.path.join(self.exportpath, filename + u'.pdf')
        with metarace.savefile(ofile) as f:
            rep.output_pdf(f)
        ofile = os.path.join(self.exportpath, filename + u'.xls')
        with metarace.savefile(ofile) as f:
            rep.output_xls(f)
        ofile = os.path.join(self.exportpath, filename + u'.json')
        with metarace.savefile(ofile) as f:
            rep.output_json(f)
        ofile = os.path.join(self.exportpath, filename + u'.html')
        with metarace.savefile(ofile) as f:
            rep.output_html(f, linkbase=lb, linktypes=lt)

    def menu_data_results_cb(self, menuitem, data=None):
        """Create live result report and export"""
        self.saveconfig()
        if self.curevent is None:
            return
        if self.lifexport:  # save current lif with export
            lifdat = self.curevent.lifexport()
            if len(lifdat) > 0:
                liffile = os.path.join(self.exportpath, u'lifexport.lif')
                with metarace.savefile(liffile) as f:
                    cw = ucsv.UnicodeWriter(f, quoting=csv.QUOTE_MINIMAL)
                    for r in lifdat:
                        cw.writerow(r)
        if self.resfiles:
            self.export_result_maker()
        glib.idle_add(self.mirror_start)

    ## Directory utilities
    def event_configfile(self, evno):
        """Return a config filename for the given event no."""
        return u'event_{}.json'.format(unicode(evno))

    ## Timing menu callbacks
    def menu_timing_subtract_activate_cb(self, menuitem, data=None):
        """Run the time of day subtraction dialog."""
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'tod_subtract.ui'))
        ste = b.get_object(u'timing_start_entry')
        fte = b.get_object(u'timing_finish_entry')
        nte = b.get_object(u'timing_net_entry')
        b.get_object(u'timing_start_now').connect(u'clicked',
                                                  self.entry_set_now, ste)
        b.get_object(u'timing_finish_now').connect(u'clicked',
                                                   self.entry_set_now, fte)
        ste.connect(u'activate', self.menu_timing_recalc, ste, fte, nte)
        fte.connect(u'activate', self.menu_timing_recalc, ste, fte, nte)
        dlg = b.get_object(u'timing')
        dlg.set_transient_for(self.window)
        dlg.run()
        dlg.destroy()

    def menu_timing_start_activate_cb(self, menuitem, data=None):
        """Manually set race elapsed time via trigger."""
        if self.curevent is None:
            LOG.info(u'No event open to set elapsed time on')
        else:
            self.curevent.elapsed_dlg()

    def entry_set_now(self, button, entry=None):
        """Enter the current time in the provided entry."""
        entry.set_text(tod.now().timestr())
        entry.activate()

    def menu_timing_recalc(self, entry, ste, fte, nte):
        """Update the net time entry for the supplied start and finish."""
        st = tod.mktod(ste.get_text())
        ft = tod.mktod(fte.get_text())
        if st is not None and ft is not None:
            ste.set_text(st.timestr())
            fte.set_text(ft.timestr())
            nte.set_text((ft - st).timestr())

    def menu_timing_clear_activate_cb(self, menuitem, data=None):
        """Clear memory in attached timing devices."""
        self.timer.clear()
        self.alttimer.clrmem()

    def menu_timing_sync_activate_cb(self, menuitem, data=None):
        """Roughly synchronise decoder."""
        self.timer.sync()
        LOG.info(u'Request timer synchronisation')

    def menu_timing_reconnect_activate_cb(self, menuitem, data=None):
        """Reconnect timer and re-start."""
        self.set_timer(self.timer_port, force=True)
        # forced reconnect on THBC requires stop, sane and start
        if self.timer_port.startswith(u'thbc'):
            self.timer.stop_session()
            self.timer.sane()
            self.timer.start_session()
        self.alttimer.setport(self.alttimer_port)
        self.alttimer.sane()
        if self.etype == u'irtt':
            # IRTT impulses require no delay
            self.alttimer.delaytime(u'0.01')
        else:
            # assume 1 second gaps at finish
            self.alttimer.write(u'DTF01.00')
        LOG.info(u'Re-connect/re-start attached timers')

    def restart_decoder(self, data=None):
        """Request re-start of decoder."""
        self.timer.start_session()
        return None

    def menu_timing_configure_activate_cb(self, menuitem, data=None):
        """Attempt to re-configure the attached decoder from saved config."""
        if self.timer_port.startswith(u'thbc'):
            if not uiutil.questiondlg(
                    self.window, u'Re-configure THBC Decoder Settings?',
                    u'Note: Passings will not be captured while decoder is updating.'
            ):
                LOG.debug(u'Config aborted')
                return False
            self.timer.stop_session()
            self.timer.sane()
            glib.timeout_add_seconds(60, self.restart_decoder)
            self.timer.ipconfig()
        else:
            LOG.info(u'Decoder config not available')
        return None

    ## Help menu callbacks
    def menu_help_about_cb(self, menuitem, data=None):
        """Display metarace about dialog."""
        metarace.about_dlg(self.window)

    ## Race Control Elem callbacks
    def race_stat_but_clicked_cb(self, button, data=None):
        """Call through into event if open."""
        if self.curevent is not None:
            self.curevent.stat_but_clicked(button)

    def race_stat_entry_activate_cb(self, entry, data=None):
        """Pass the chosen action and bib list through to curevent."""
        action = self.action_model.get_value(
            self.action_combo.get_active_iter(), 0)
        if self.curevent is not None:
            if self.curevent.race_ctrl(
                    action,
                    self.action_entry.get_text().decode(u'utf-8')):
                self.action_entry.set_text(u'')

    ## Menu button callbacks
    def race_action_combo_changed_cb(self, combo, data=None):
        """Notify curevent of change in combo."""
        aiter = self.action_combo.get_active_iter()
        if self.curevent is not None and aiter is not None:
            action = self.action_model.get_value(aiter, 0)
            self.curevent.ctrl_change(action, self.action_entry)

    def menu_rfustat_clicked_cb(self, button, data=None):
        LOG.info(u'Request timer status')
        self.timer.status()
        self.alttimer.status()

    def menu_clock_clicked_cb(self, button, data=None):
        """Handle click on menubar clock."""
        LOG.info(u'PC ToD: %s', tod.now().rawtime())

    ## 'Slow' Timer callback - this is the main ui event routine
    def timeout(self):
        """Update status buttons and time of day clock button."""
        if self.running:
            # update pc ToD label
            self.clock_label.set_text(tod.now().meridiem())

            # call into race timeout handler
            if self.curevent is not None:
                self.curevent.timeout()

            # check for completion in the export thread
            if self.mirror is not None:
                if not self.mirror.is_alive():
                    self.mirror = None
                    LOG.debug(u'Removing completed export thread.')
        else:
            return False
        return True

    ## Window methods
    def set_title(self, extra=u''):
        """Update window title from meet properties."""
        typepfx = u''
        title = self.title_str.strip()
        if self.etype in ROADRACE_TYPES:
            typepfx = ROADRACE_TYPES[self.etype] + u': '
        self.window.set_title(typepfx + title)
        if self.curevent is not None:
            self.curevent.set_titlestr(self.subtitle_str)

    def meet_destroy_cb(self, window, msg=u''):
        """Handle destroy signal and exit application."""
        rootlogger = logging.getLogger()
        rootlogger.removeHandler(self.sh)
        rootlogger.removeHandler(self.lh)
        self.window.hide()
        glib.idle_add(self.meet_destroy_handler)

    def meet_destroy_handler(self):
        if self.curevent is not None:
            self.close_event()
        if self.started:
            self.saveconfig()
            self.shutdown()  # threads are joined in shutdown
        rootlogger = logging.getLogger()
        if self.loghandler is not None:
            rootlogger.removeHandler(self.loghandler)
        self.running = False
        gtk.main_quit()
        return False

    def key_event(self, widget, event):
        """Collect key events on main window and send to race."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or u'None'  # str
            if event.state & gtk.gdk.CONTROL_MASK:
                key = key.lower()
                t = tod.now(chan=u'MAN', refid=unicode(key))
                if key in ['0', '1']:
                    # trigger
                    t.refid = u''
                    t.chan = strops.id2chan(strops.chan2id(key))
                    self._alttimercb(t)
                    return True
                elif key in ['2', '3', '4', '5', '6', '7', '8', '9']:
                    # passing
                    self._timercb(t)
                    return True
            if self.curevent is not None:
                return self.curevent.key_event(widget, event)
        return False

    def shutdown(self, msg=u''):
        """Shutdown worker threads and close application."""
        self.started = False
        self.announce.exit(msg)
        self.timer.exit(msg)
        self.alttimer.exit(msg)
        LOG.info(u'Waiting for workers')
        if self.mirror is not None:
            LOG.debug(u'Result export')
            self.mirror.join()
            self.mirror = None
        LOG.debug(u'Telegraph/announce')
        self.announce.join()

    def start(self):
        """Start the timer and rfu threads."""
        if not self.started:
            LOG.debug(u'Meet startup')
            self.announce.start()
            self.timer.start()
            self.alttimer.start()
            self.started = True

    ## Roadmeet functions
    def saveconfig(self):
        """Save current meet data to disk."""
        if self.curevent is not None and self.curevent.winopen:
            self.curevent.saveconfig()
        cw = jsonconfig.config()
        cw.add_section(u'roadmeet')
        cw.set(u'roadmeet', u'id', ROADMEET_ID)
        cw.set(u'roadmeet', u'anntopic', self.anntopic)
        cw.set(u'roadmeet', u'timertopic', self.timertopic)
        cw.set(u'roadmeet', u'remote_enable', self.remote_enable)
        cw.set(u'roadmeet', u'timer', self.timer_port)
        cw.set(u'roadmeet', u'alttimer', self.alttimer_port)
        cw.set(u'roadmeet', u'shortname', self.shortname)
        cw.set(u'roadmeet', u'linkbase', self.linkbase)
        cw.set(u'roadmeet', u'indexlink', self.indexlink)
        cw.set(u'roadmeet', u'nextlink', self.nextlink)
        cw.set(u'roadmeet', u'prevlink', self.prevlink)
        cw.set(u'roadmeet', u'title', self.title_str)
        cw.set(u'roadmeet', u'subtitle', self.subtitle_str)
        cw.set(u'roadmeet', u'document', self.document_str)
        cw.set(u'roadmeet', u'date', self.date_str)
        cw.set(u'roadmeet', u'organiser', self.organiser_str)
        cw.set(u'roadmeet', u'commissaire', self.commissaire_str)

        cw.set(u'roadmeet', u'resultnos', self.bibs_in_results)
        cw.set(u'roadmeet', u'lifexport', self.lifexport)
        cw.set(u'roadmeet', u'resfiles', self.resfiles)
        cw.set(u'roadmeet', u'provisionalstart', self.provisionalstart)
        cw.set(u'roadmeet', u'distance', self.distance)
        cw.set(u'roadmeet', u'diststr', self.diststr)
        cw.set(u'roadmeet', u'docindex', self.docindex)
        cw.set(u'roadmeet', u'mirrorpath', self.mirrorpath)
        cw.set(u'roadmeet', u'mirrorcmd', self.mirrorcmd)
        cw.set(u'roadmeet', u'mirrorfile', self.mirrorfile)
        cw.set(u'roadmeet', u'competitioncode', self.competitioncode)
        cw.set(u'roadmeet', u'eventcode', self.eventcode)
        cw.set(u'roadmeet', u'racetype', self.racetype)
        cw.set(u'roadmeet', u'competitortype', self.competitortype)
        cw.set(u'roadmeet', u'documentversion', self.documentversion)

        with metarace.savefile(CONFIGFILE) as f:
            cw.write(f)
        self.rdb.save(u'riders.csv')
        self.edb.save(u'events.csv')
        LOG.info(u'Meet configuration saved')

    def set_timer(self, newdevice=u'', force=False):
        """Re-set the main timer device and connect callback."""
        if newdevice != self.timer_port or force:
            self.timer = decoder.mkdevice(newdevice, self.timer)
            self.timer_port = newdevice
        else:
            LOG.debug(u'set_timer - No change required')
        self.timer.setcb(self._timercb)

    def loadconfig(self):
        """Load meet config from disk."""
        cr = jsonconfig.config({
            u'roadmeet': {
                u'shortname': None,
                u'title': u'',
                u'subtitle': u'',
                u'document': u'',
                u'date': u'',
                u'organiser': u'',
                u'commissaire': u'',
                u'distance': None,
                u'diststr': u'',
                u'docindex': u'0',
                u'timer': u'',
                u'alttimer': u'',
                u'resultnos': True,
                u'anntopic': None,
                u'timertopic': None,
                u'remote_enable': False,
                u'linkbase': u'.',
                u'indexlink': None,
                u'nextlink': None,
                u'prevlink': None,
                u'lifexport': False,
                u'resfiles': True,
                u'provisionalstart': False,
                u'mirrorpath': u'',
                u'mirrorcmd': u'echo',
                u'mirrorfile': u'',
                u'competitioncode': u'',
                u'eventcode': u'',
                u'racetype': u'',
                u'competitortype': u'',
                u'documentversion': u'',
                u'id': u''
            }
        })
        cr.add_section(u'roadmeet')
        cr.merge(metarace.sysconf, u'roadmeet')
        LOG.debug(u'Load system meet defaults')

        # re-set main log file
        LOG.debug(u'Adding meet logfile handler %r', LOGFILE)
        rootlogger = logging.getLogger()
        if self.loghandler is not None:
            rootlogger.removeHandler(self.loghandler)
            self.loghandler.close()
            self.loghandler = None
        self.loghandler = logging.FileHandler(LOGFILE)
        self.loghandler.setLevel(LOGFILE_LEVEL)
        self.loghandler.setFormatter(logging.Formatter(metarace.LOGFILEFORMAT))
        rootlogger.addHandler(self.loghandler)

        # check for config file
        if os.path.exists(CONFIGFILE):
            try:
                with open(CONFIGFILE, 'rb') as f:
                    cr.read(f)
                LOG.debug(u'Read meet config from %r', CONFIGFILE)
            except Exception as e:
                LOG.error(u'Unable to read meet config: %s', e)

        # set timer port (decoder)
        self.set_timer(cr.get(u'roadmeet', u'timer'))

        # set alt timer port (timy)
        nport = cr.get(u'roadmeet', u'alttimer')
        if nport != self.alttimer_port:
            self.alttimer_port = nport
            self.alttimer.setport(nport)
            self.alttimer.sane()  # sane prod here is probably good idea

        # set the default announce topic and subscribe to control topic
        self.anntopic = cr.get(u'roadmeet', u'anntopic')
        if self.anntopic:
            self.announce.subscribe(u'/'.join(
                (self.anntopic, u'control', u'#')))

        # fetch the remote timer topic and update remote control
        self.timertopic = cr.get(u'roadmeet', u'timertopic')
        self.remote_enable = cr.get_bool(u'roadmeet', u'remote_enable')
        self.remote_reset()

        # set meet meta, and then copy into text entries
        self.shortname = cr.get(u'roadmeet', u'shortname')
        self.title_str = cr.get(u'roadmeet', u'title')
        self.subtitle_str = cr.get(u'roadmeet', u'subtitle')
        self.document_str = cr.get(u'roadmeet', u'document')
        self.date_str = cr.get(u'roadmeet', u'date')
        self.organiser_str = cr.get(u'roadmeet', u'organiser')
        self.commissaire_str = cr.get(u'roadmeet', u'commissaire')
        self.distance = cr.get_float(u'roadmeet', u'distance')
        self.diststr = cr.get(u'roadmeet', u'diststr')
        self.docindex = cr.get_posint(u'roadmeet', u'docindex', 0)
        self.linkbase = cr.get(u'roadmeet', u'linkbase')
        self.indexlink = cr.get(u'roadmeet', u'indexlink')
        self.prevlink = cr.get(u'roadmeet', u'prevlink')
        self.nextlink = cr.get(u'roadmeet', u'nextlink')
        self.bibs_in_results = cr.get_bool(u'roadmeet', u'resultnos')
        self.mirrorpath = cr.get(u'roadmeet', u'mirrorpath')
        self.mirrorcmd = cr.get(u'roadmeet', u'mirrorcmd')
        self.mirrorfile = cr.get(u'roadmeet', u'mirrorfile')
        self.competitioncode = cr.get(u'roadmeet', u'competitioncode')
        self.eventcode = cr.get(u'roadmeet', u'eventcode')
        self.racetype = cr.get(u'roadmeet', u'racetype')
        self.competitortype = cr.get(u'roadmeet', u'competitortype')
        self.documentversion = cr.get(u'roadmeet', u'documentversion')
        self.lifexport = cr.get_bool(u'roadmeet', u'lifexport')
        self.resfiles = cr.get_bool(u'roadmeet', u'resfiles')
        self.provisionalstart = cr.get_bool(u'roadmeet', u'provisionalstart')

        # Re-Initialise rider and event databases
        self.rdb.clear()
        self.edb.clear()
        self.rdb.load(u'riders.csv')
        self.edb.load(u'events.csv')
        event = self.edb.getfirst()
        if event is None:  # add a new event of the right type
            event = self.edb.add_empty(evno=u'0')
            event[u'type'] = self.etype
        else:
            self.etype = event[u'type']
            LOG.debug(u'Existing event in db: %r', self.etype)
        self.open_event(event)  # always open on load if posible
        self.set_title()

        # alt timer config post event load
        if self.etype == u'irtt':
            # IRTT impulses require no delay
            self.alttimer.delaytime(u'0.01')
        else:
            # assume 1 second gaps at finish
            self.alttimer.write(u'DTF01.00')

        # make sure export path exists
        if not os.path.exists(self.exportpath):
            os.mkdir(self.exportpath)
            LOG.info(u'Created export path: %r', self.exportpath)

        # check and warn of config mismatch
        cid = cr.get(u'roadmeet', u'id')
        if cid != ROADMEET_ID:
            LOG.warning(u'Meet config mismatch: %r != %r', cid, ROADMEET_ID)

    def get_distance(self):
        """Return race distance in km."""
        return self.distance

    ## Announcer methods (replaces old irc/unt telegraph)
    def cmd_announce(self, command, msg):
        """Announce the supplied message to the command topic."""
        if self.anntopic:
            topic = u'/'.join((self.anntopic, command))
            self.announce.publish(msg, topic)

    def rider_announce(self, rvec):
        """Issue a serialised rider vector to announcer."""
        # Deprecated UNT-style list
        self.cmd_announce(u'rider', u'\x1f'.join(rvec))

    def timer_announce(self, evt, timer=None, source=u''):
        """Send message into announce for remote control."""
        if not self.remote_enable and self.timertopic is not None:
            if timer is None:
                timer = self.timer
            prec = 4
            if timer is self.timer:
                prec = 3  # transponders have reduced precision
            elif u'M' in evt.chan:
                prec = 3
            if evt.source is not None:
                source = evt.source
            tvec = [evt.index, source, evt.chan, evt.refid, evt.rawtime(prec)]
            self.announce.publish(u';'.join(tvec), self.timertopic)
        return False

    def mirror_start(self):
        """Create a new mirror thread unless already in progress."""
        if self.mirrorpath and self.mirror is None:
            self.mirror = export.mirror(localpath=os.path.join(u'export', u''),
                                        remotepath=self.mirrorpath,
                                        mirrorcmd=self.mirrorcmd)
            self.mirror.start()
        return False  # for idle_add

    def remote_reset(self):
        """Reset remote input of timer messages."""
        LOG.debug(u'Remote control reset')
        if self.timertopic is not None:
            if self.remote_enable:
                LOG.debug(u'Listening for remote timer at %r', self.timertopic)
                self.announce.subscribe(self.timertopic)
            else:
                LOG.debug(u'Remote timer disabled')
                self.announce.unsubscribe(self.timertopic)
        else:
            LOG.debug(u'Remote timer topic not cofigured')

    def remote_timer(self, msg):
        """Process and dispatch a remote timer message."""
        # 'INDEX;SOURCE;CHANNEL;REFID;TIMEOFDAY'
        tv = msg.split(u';')
        if len(tv) == 5:
            try:
                tval = tod.mktod(tv[4])
                tval.source = tv[1]
                tval.chan = tv[2]
                tval.refid = tv[3]
                LOG.debug(u'Remote src:%r index:%r chan:%r refid:%r @ %r',
                          tv[1], tv[0], tv[2], tv[3], tval.rawtime())
                if u'timy' in tv[1]:
                    tval.index = tv[0]
                    self._alttimercb(tval)
                else:
                    tval.index = u'REM'
                    self._timercb(tval)
            except Exception as e:
                LOG.warning(u'Error reading timer msg %r: %s', msg, e)
        else:
            LOG.debug(u'Invalid remote timer message: %r', tv)

    def remote_command(self, topic, msg):
        """Handle a remote control message."""
        if topic == self.timertopic:
            if self.remote_enable:
                self.remote_timer(msg)
        else:
            LOG.debug(u'Unsupported remote command %r:%r', topic, msg)
        return False

    def _timercb(self, evt, data=None):
        if self.timercb is not None:
            glib.idle_add(self.timercb, evt, priority=glib.PRIORITY_HIGH)
        glib.idle_add(self.timer_announce, evt, self.timer, u'rfid')

    def _alttimercb(self, evt, data=None):
        if self.alttimercb is not None:
            glib.idle_add(self.alttimercb, evt, priority=glib.PRIORITY_HIGH)
        glib.idle_add(self.timer_announce, evt, self.alttimer, u'timy')

    def _controlcb(self, topic=None, message=None):
        glib.idle_add(self.remote_command, topic, message)

    def __init__(self, etype=None):
        """Meet constructor."""
        self.loghandler = None  # set in loadconfig to meet dir
        self.exportpath = EXPORTPATH
        if etype not in ROADRACE_TYPES:
            etype = u'road'
        self.etype = etype
        self.shortname = None
        self.title_str = u''
        self.subtitle_str = u''
        self.document_str = u''
        self.date_str = u''
        self.organiser_str = u''
        self.commissaire_str = u''
        self.distance = None
        self.diststr = u''
        self.docindex = 0
        self.linkbase = u'.'
        self.provisionalstart = False
        self.indexlink = None
        self.nextlink = None
        self.prevlink = None

        self.bibs_in_results = True
        self.remote_enable = False
        self.lifexport = False
        self.resfiles = True

        # printer preferences
        paper = gtk.paper_size_new_custom('metarace-full', 'A4 for reports',
                                          595, 842, gtk.UNIT_POINTS)
        self.printprefs = gtk.PrintSettings()
        self.pageset = gtk.PageSetup()
        self.pageset.set_orientation(gtk.PAGE_ORIENTATION_PORTRAIT)
        self.pageset.set_paper_size(paper)
        self.pageset.set_top_margin(0, gtk.UNIT_POINTS)
        self.pageset.set_bottom_margin(0, gtk.UNIT_POINTS)
        self.pageset.set_left_margin(0, gtk.UNIT_POINTS)
        self.pageset.set_right_margin(0, gtk.UNIT_POINTS)

        # hardware connections
        self.timertopic = None  # remote timer topic
        self.timer = decoder.decoder()
        self.timer_port = u''
        self.timer.setcb(self._timercb)
        self.timercb = None  # set by event app
        self.alttimer = timy.timy()  # alttimer is always timy
        self.alttimer_port = u''
        self.alttimer.setcb(self._alttimercb)
        self.alttimercb = None  # set by event app
        self.announce = telegraph.telegraph()
        self.announce.setcb(self._controlcb)
        self.anntopic = None
        self.mirrorpath = u''
        self.mirrorcmd = u'echo'
        self.mirrorfile = u''
        self.mirror = None
        self.competitioncode = u''
        self.eventcode = u''
        self.racetype = u''
        self.competitortype = u''
        self.documentversion = u''

        uifile = os.path.join(metarace.UI_PATH, u'roadmeet.ui')
        LOG.debug(u'Building user interface from %r', uifile)
        b = gtk.Builder()
        b.add_from_file(uifile)
        self.window = b.get_object(u'meet')
        self.window.connect(u'key-press-event', self.key_event)
        self.clock = b.get_object(u'menu_clock')
        self.clock_label = b.get_object(u'menu_clock_label')
        self.menu_rfustat_img = b.get_object(u'menu_rfustat_img')
        self.status = b.get_object(u'status')
        self.log_buffer = b.get_object(u'log_buffer')
        self.log_view = b.get_object(u'log_view')
        self.log_view.modify_font(uiutil.LOGVIEWFONT)
        self.log_scroll = b.get_object(u'log_box').get_vadjustment()
        self.context = self.status.get_context_id(u'metarace meet')
        self.menu_race_close = b.get_object(u'menu_race_close')
        self.menu_race_abort = b.get_object(u'menu_race_abort')
        self.decoder_configure = b.get_object(u'menu_timing_configure')
        self.race_box = b.get_object(u'race_box')
        self.stat_but = uiutil.statbut(b.get_object(u'race_stat_but'))
        self.action_model = b.get_object(u'race_action_model')
        self.action_combo = b.get_object(u'race_action_combo')
        self.action_entry = b.get_object(u'race_action_entry')
        b.get_object(u'race_stat_hbox').set_focus_chain(
            [self.action_combo, self.action_entry, self.action_combo])

        # prepare local scratch pad ? can these be removed?
        self.an_cur_lap = tod.ZERO
        self.an_cur_split = tod.ZERO
        self.an_cur_bunchid = 0
        self.an_cur_bunchcnt = 0
        self.an_last_time = None
        self.an_cur_start = tod.ZERO

        b.connect_signals(self)

        # run state
        self.running = True
        self.started = False
        self.curevent = None

        # connect UI log handlers
        LOG.debug(u'Connecting interface log handlers')
        rootlogger = logging.getLogger()
        f = logging.Formatter(metarace.LOGFORMAT)
        self.sh = loghandler.statusHandler(self.status, self.context)
        self.sh.setFormatter(f)
        self.sh.setLevel(logging.INFO)  # show info+ on status bar
        rootlogger.addHandler(self.sh)
        self.lh = loghandler.textViewHandler(self.log_buffer, self.log_view,
                                             self.log_scroll)
        self.lh.setFormatter(f)
        self.lh.setLevel(logging.INFO)  # show info+ in text view
        rootlogger.addHandler(self.lh)

        # get rider db and pack into a dialog
        LOG.debug(u'Add riderdb and eventdb')
        self.rdb = riderdb.riderdb()
        b.get_object(u'riders_box').add(
            self.rdb.mkview(cat=True,
                            series=True,
                            refid=True,
                            ucicode=True,
                            note=True))
        # get event db -> loadconfig adds empty event if one not present
        self.edb = eventdb.eventdb([])

        # select event page in notebook.
        b.get_object(u'meet_nb').set_current_page(0)

        # start timer
        glib.timeout_add_seconds(1, self.timeout)


def main():
    """Run the road meet application as a console script."""
    # attach a console log handler to the root logger then run the ui
    ch = logging.StreamHandler()
    ch.setLevel(metarace.LOGLEVEL)
    fh = logging.Formatter(metarace.LOGFORMAT)
    ch.setFormatter(fh)
    logging.getLogger().addHandler(ch)

    metarace.init(withgtk=True)
    configpath = metarace.DATA_PATH
    if len(sys.argv) > 2:
        LOG.error(u'Usage: roadmeet [configdir]')
        sys.exit(1)
    elif len(sys.argv) == 2:
        configpath = sys.argv[1]
    configpath = metarace.config_path(configpath)
    if configpath is None:
        LOG.error(u'Unable to open meet config %r', sys.argv[1])
        sys.exit(1)
    app = runapp(configpath)
    try:
        metarace.mainloop()
    except:
        app.shutdown(u'Exception from main loop')
        raise
    return 0


def runapp(configpath, etype=None):
    """Create the roadmeet object, start in configpath and return a handle."""
    lf = metarace.lockpath(configpath)
    if lf is None:
        LOG.error(u'Unable to lock meet config, already in use')
        sys.exit(1)
    LOG.debug(u'Entering meet folder %r', configpath)
    os.chdir(configpath)
    app = roadmeet(etype)
    app.loadconfig()
    app.window.show()
    app.start()
    return app


if __name__ == u'__main__':
    main()
