# SPDX-License-Identifier: MIT
"""Timing and data handling application for track meets."""

import pygtk

pygtk.require("2.0")

import gtk
import glib

import os
import sys
import logging
import socket
import threading

import metarace

from metarace import jsonconfig
from metarace import tod
from metarace import riderdb
from metarace import eventdb
from metarace import scbwin
from metarace import sender
from metarace import telegraph
from metarace import export
from metarace import timy
from metarace import gemini
from metarace import unt4
from metarace import strops
from metarace import loghandler
from metarace import race
from metarace import f200
from metarace import classification
from metarace import ps
from metarace import ittt
from metarace import hour
from metarace import sprnd
from metarace import report
from metarace import uiutil

LOGFILE = u'event.log'
LOGFILE_LEVEL = logging.INFO  # check
CONFIGFILE = u'config.json'
TRACKMEET_ID = u'trackmeet_1.10'  # configuration versioning
EXPORTPATH = u'export'
MAXREP = 10000  # communique max number
SESSBREAKTHRESH = 0.075  # forced page break threshold
ANNOUNCE_LINELEN = 80  # length of lines on text-only DHI announcer
_log = logging.getLogger(u'metarace.trackmeet')
_log.setLevel(logging.DEBUG)


def mkrace(meet, event, ui=True):
    """Return a race object of the correct type."""
    ret = None
    etype = event[u'type']
    if etype in [
            u'indiv tt', u'indiv pursuit', u'pursuit race', u'team pursuit',
            u'team pursuit race'
    ]:
        ret = ittt.ittt(meet, event, ui)
    elif etype in [u'points', u'madison', u'omnium']:
        ret = ps.ps(meet, event, ui)
    elif etype == u'classification':
        ret = classification.classification(meet, event, ui)
    elif etype in [u'flying 200', u'flying lap']:
        ret = f200.f200(meet, event, ui)
    elif etype in [u'hour']:
        ret = hour.hourrec(meet, event, ui)
    elif etype in [u'sprint round', u'sprint final']:
        ret = sprnd.sprnd(meet, event, ui)
    else:
        ret = race.race(meet, event, ui)
    return ret


class trackmeet(object):
    """Track meet application class."""

    ## Meet Menu Callbacks
    def get_event(self, evno, ui=False):
        """Return an event object for the given event number."""
        # NOTE: returned event will need to be destroyed
        ret = None
        eh = self.edb[evno]
        if eh is not None:
            ret = mkrace(self, eh, ui)
        return ret

    def menu_meet_save_cb(self, menuitem, data=None):
        """Save current meet data and open event."""
        self.saveconfig()

    def menu_meet_info_cb(self, menuitem, data=None):
        """Display meet information on scoreboard."""
        self.gemini.clear()
        self.clock.clicked()

    def menu_meet_properties_cb(self, menuitem, data=None):
        """Edit meet properties."""
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'trackmeet_props.ui'))
        dlg = b.get_object(u'properties')
        dlg.set_transient_for(self.window)

        # load meet meta
        tent = b.get_object(u'meet_title_entry')
        tent.set_text(self.titlestr)
        stent = b.get_object(u'meet_subtitle_entry')
        stent.set_text(self.subtitlestr)
        dent = b.get_object(u'meet_date_entry')
        dent.set_text(self.datestr)
        lent = b.get_object(u'meet_loc_entry')
        lent.set_text(self.locstr)
        cent = b.get_object(u'meet_comm_entry')
        cent.set_text(self.commstr)
        oent = b.get_object(u'meet_org_entry')
        oent.set_text(self.orgstr)

        # load data/result opts
        re = b.get_object(u'data_showevno')
        re.set_active(self.showevno)
        cm = b.get_object(u'data_clubmode')
        cm.set_active(self.clubmode)
        prov = b.get_object(u'data_provisional')
        prov.set_active(self.provisional)
        tln = b.get_object(u'tracklen_total')
        tln.set_value(self.tracklen_n)
        tld = b.get_object(u'tracklen_laps')
        tldl = b.get_object(u'tracklen_lap_label')
        tld.connect(u'value-changed', self.tracklen_laps_value_changed_cb,
                    tldl)
        tld.set_value(self.tracklen_d)

        # scb/timing ports
        spe = b.get_object(u'scb_port_entry')
        if self.scbport is not None:
            spe.set_text(self.scbport)
        upe = b.get_object(u'uscb_port_entry')
        if self.anntopic is not None:
            upe.set_text(self.anntopic)
        spb = b.get_object(u'scb_port_dfl')
        spb.connect(u'clicked', self.set_default, spe, u'DEFAULT')
        mte = b.get_object(u'timing_main_entry')
        if self.main_port is not None:
            mte.set_text(self.main_port)
        mtb = b.get_object(u'timing_main_dfl')
        mtb.connect(u'clicked', self.set_default, mte, u'DEFAULT')

        # run dialog
        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            _log.debug(u'Updating meet properties')

            # update meet meta
            self.titlestr = tent.get_text().decode('utf-8', 'replace')
            self.subtitlestr = stent.get_text().decode('utf-8', 'replace')
            self.datestr = dent.get_text().decode('utf-8', 'replace')
            self.locstr = lent.get_text().decode('utf-8', 'replace')
            self.commstr = cent.get_text().decode('utf-8', 'replace')
            self.orgstr = oent.get_text().decode('utf-8', 'replace')
            self.set_title()

            self.clubmode = cm.get_active()
            self.showevno = re.get_active()
            self.provisional = prov.get_active()
            self.tracklen_n = tln.get_value_as_int()
            self.tracklen_d = tld.get_value_as_int()
            nport = spe.get_text().decode('utf-8', 'replace')
            if nport != self.scbport:
                # TODO: swap type handler if necessary
                self.scbport = nport
                self.scb.setport(nport)
            nport = upe.get_text().decode('utf-8', 'replace')
            if nport != self.anntopic:
                if self.anntopic is not None:
                    self.announce.unsubscribe(u'/'.join(
                        (self.anntopic, u'control', u'#')))
                self.anntopic = None
                if nport:
                    self.anntopic = nport
                    self.announce.subscribe(u'/'.join(
                        (self.anntopic, u'control', u'#')))
            nport = mte.get_text().decode('utf-8', 'replace')
            if nport != self.main_port:
                self.main_port = nport
                self.main_timer.setport(nport)
            _log.debug(u'Properties updated')
        else:
            _log.debug(u'Edit properties cancelled')
        dlg.destroy()

    def menu_meet_fullscreen_toggled_cb(self, button, data=None):
        """Update fullscreen window view."""
        if button.get_active():
            self.window.fullscreen()
        else:
            self.window.unfullscreen()

    def tracklen_laps_value_changed_cb(self, spin, lbl):
        """Laps changed in properties callback."""
        if int(spin.get_value()) > 1:
            lbl.set_text(u' laps = ')
        else:
            lbl.set_text(u' lap = ')

    def set_default(self, button, dest, val):
        """Update dest to default value val."""
        dest.set_text(val)

    def menu_meet_quit_cb(self, menuitem, data=None):
        """Quit the track meet application."""
        self.running = False
        self.window.destroy()

    ## Report print support
    def print_report(self,
                     sections=[],
                     subtitle=u'',
                     docstr=u'',
                     prov=False,
                     doprint=True,
                     exportfile=None):
        """Print the suuplied sections in a standard report."""
        _log.info(u'Printing report %s %s', subtitle, docstr)

        rep = report.report()
        rep.set_provisional(prov)
        rep.strings[u'title'] = self.titlestr
        rep.strings[u'subtitle'] = (self.subtitlestr + u' ' + subtitle).strip()
        rep.strings[u'datestr'] = strops.promptstr(u'Date:', self.datestr)
        rep.strings[u'commstr'] = strops.promptstr(u'Chief Commissaire:',
                                                   self.commstr)
        rep.strings[u'orgstr'] = strops.promptstr(u'Organiser: ', self.orgstr)
        rep.strings[u'docstr'] = docstr
        rep.strings[u'diststr'] = self.locstr
        for sec in sections:
            rep.add_section(sec)

        # write out to files if exportfile set
        if exportfile:
            ofile = os.path.join(self.exportpath, exportfile + u'.pdf')
            with metarace.savefile(ofile) as f:
                rep.output_pdf(f)
            ofile = os.path.join(self.exportpath, exportfile + u'.xls')
            with metarace.savefile(ofile) as f:
                rep.output_xls(f)
            ofile = os.path.join(self.exportpath, exportfile + u'.json')
            with metarace.savefile(ofile) as f:
                rep.output_json(f)
            lb = u''
            lt = []
            if self.mirrorpath:
                lb = os.path.join(self.linkbase, exportfile)
                lt = [u'pdf', u'xls']
            ofile = os.path.join(self.exportpath, exportfile + u'.html')
            with metarace.savefile(ofile) as f:
                rep.output_html(f, linkbase=lb, linktypes=lt)

        if not doprint:
            return False

        print_op = gtk.PrintOperation()
        print_op.set_allow_async(True)
        print_op.set_print_settings(self.printprefs)
        print_op.set_default_page_setup(self.pageset)
        print_op.connect(u'begin_print', self.begin_print, rep)
        print_op.connect(u'draw_page', self.draw_print_page, rep)
        _log.debug(u'Calling into print_op.run()')
        res = print_op.run(gtk.PRINT_OPERATION_ACTION_PREVIEW, None)
        if res == gtk.PRINT_OPERATION_RESULT_APPLY:
            self.printprefs = print_op.get_print_settings()
            _log.debug(u'Updated print preferences')
        elif res == gtk.PRINT_OPERATION_RESULT_IN_PROGRESS:
            _log.debug(u'Print operation in progress')

        # may be called via idle_add
        return False

    def begin_print(self, operation, context, rep):
        """Set print pages and units."""
        rep.start_gtkprint(context.get_cairo_context())
        operation.set_n_pages(rep.get_pages())
        operation.set_unit("points")

    def draw_print_page(self, operation, context, page_nr, rep):
        """Draw to the nominated page."""
        rep.set_context(context.get_cairo_context())
        rep.draw_page(page_nr)

    def find_communique(self, lookup):
        """Find or allocate a communique number."""
        ret = None
        cnt = 1
        noset = set()
        for c in self.commalloc:
            if c == lookup:  # previous allocation
                ret = self.commalloc[c]
                _log.debug(u'Found allocation: ' + ret + u' -> ' + lookup)
                break
            else:
                noset.add(self.commalloc[c])
        if ret is None:  # not yet allocated
            while True:
                ret = unicode(cnt)
                if ret not in noset:
                    self.commalloc[lookup] = ret  # write back
                    _log.debug(u'Add allocation: ' + ret + u' -> ' + lookup)
                    break
                else:
                    cnt += 1
                    if cnt > MAXREP:
                        _log.error(u'Gave up looking for communique no')
                        break  # safer
        return ret

    ## Event action callbacks
    def eventdb_cb(self, evlist, reptype=None):
        """Make a report containing start lists for the events listed."""
        # Note: selections via event listing override extended properties
        #       even if the selection does not really make sense, this
        #       allows for creation of reports manually crafted.
        secs = []
        reptypestr = reptype.title()
        lsess = None
        for eno in evlist:
            e = self.edb[eno]
            nsess = e['sess']
            if nsess != lsess and lsess is not None:
                secs.append(report.pagebreak(SESSBREAKTHRESH))
            lsess = nsess
            h = mkrace(self, e, False)
            h.loadconfig()
            if reptype == u'startlist':
                secs.extend(h.startlist_report())
            elif reptype == u'result':
                reptypestr = u'Results'
                # from event list only include the individual events
                secs.extend(h.result_report(recurse=False))
            elif reptype == u'program':
                reptypestr = u'Program of Events'
                secs.extend(h.startlist_report(True))  # startlist in program
            else:
                _log.error(u'Unknown report type in eventdb calback: ' +
                           repr(reptype))
            h.destroy()
            secs.append(report.pagebreak())
        if len(secs) > 0:
            reporthash = reptype + u', '.join(evlist)
            if self.communiques:  # prompt for communique no
                #commno = uiutil.communique_dialog(self.meet.window)

                #if commno is not None and len(commno) > 1:
                gtk.gdk.threads_enter()
                rvec = uiutil.edit_times_dlg(self.window, stxt='', ftxt='')
                gtk.gdk.threads_leave()
                if len(rvec) > 1 and rvec[0] == 1:
                    commno = self.find_communique(reporthash)
                    if rvec[1]:  # it's a revision
                        commno += rvec[1]
                    if commno is not None:
                        reptypestr = (u'Communiqu\u00e9 ' + commno + u': ' +
                                      reptypestr)
                    if rvec[2]:
                        msgsec = report.bullet_text()
                        msgsec.subheading = u'Revision ' + repr(rvec[1])
                        msgsec.lines.append([u'', rvec[2]])
                        ## signature
                        secs.append(msgsec)
            self.print_report(secs,
                              docstr=reptypestr,
                              exportfile=u'trackmeet_' + reptype)
        else:
            _log.info(reptype + u' callback: Nothing to report')
        return False

    ## Race menu callbacks.
    def menu_race_startlist_activate_cb(self, menuitem, data=None):
        """Generate a startlist."""
        sections = []
        if self.curevent is not None:
            sections.extend(self.curevent.startlist_report())
        self.print_report(sections)

    def menu_race_result_activate_cb(self, menuitem, data=None):
        """Generate a result."""
        sections = []
        if self.curevent is not None:
            sections.extend(self.curevent.result_report())
        self.print_report(sections, u'Result')

    def menu_race_make_activate_cb(self, menuitem, data=None):
        """Create and open a new race of the chosen type."""
        event = self.edb.add_empty()
        event[u'type'] = data
        # Backup an existing config
        oldconf = self.event_configfile(event[u'evid'])
        if os.path.isfile(oldconf):
            # There is already a config file for this event id
            bakfile = oldconf + u'.old'
            _log.info(u'Existing config saved to %r', bakfile)
            os.rename(oldconf, bakfile)  ## TODO: replace with shutil
        self.open_event(event)
        self.menu_race_properties.activate()

    def menu_race_info_activate_cb(self, menuitem, data=None):
        """Show race information on scoreboard."""
        if self.curevent is not None:
            self.scbwin = None
            eh = self.curevent.event
            if self.showevno and eh[u'type'] not in [u'break', u'session']:
                self.scbwin = scbwin.scbclock(self.scb,
                                              u'Event ' + eh[u'evid'],
                                              eh[u'pref'], eh[u'info'])
            else:
                self.scbwin = scbwin.scbclock(self.scb, eh[u'pref'],
                                              eh[u'info'])
            self.scbwin.reset()
            self.curevent.delayed_announce()

    def menu_race_properties_activate_cb(self, menuitem, data=None):
        """Edit properties of open race if possible."""
        if self.curevent is not None:
            self.curevent.do_properties()

    def menu_race_run_activate_cb(self, menuitem=None, data=None):
        """Open currently selected event."""
        eh = self.edb.getselected()
        if eh is not None:
            self.open_event(eh)

    def event_row_activated_cb(self, view, path, col, data=None):
        """Respond to activate signal on event row."""
        self.menu_race_run_activate_cb()

    def menu_race_next_activate_cb(self, menuitem, data=None):
        """Open the next event on the program."""
        if self.curevent is not None:
            nh = self.edb.getnextrow(self.curevent.event)
            if nh is not None:
                self.open_event(nh)
            else:
                _log.warning(u'No next event to open')
        else:
            eh = self.edb.getselected()
            if eh is not None:
                self.open_event(eh)
            else:
                _log.warning(u'No next event to open')

    def menu_race_prev_activate_cb(self, menuitem, data=None):
        """Open the previous event on the program."""
        if self.curevent is not None:
            ph = self.edb.getprevrow(self.curevent.event)
            if ph is not None:
                self.open_event(ph)
            else:
                _log.warning(u'No previous event to open')
        else:
            eh = self.edb.getselected()
            if eh is not None:
                self.open_event(eh)
            else:
                _log.warning(u'No previous event to open')

    def menu_race_close_activate_cb(self, menuitem, data=None):
        """Close currently open event."""
        self.close_event()

    def menu_race_abort_activate_cb(self, menuitem, data=None):
        """Close currently open event without saving."""
        if self.curevent is not None:
            self.curevent.readonly = True
        self.close_event()

    def open_event(self, eventhdl=None):
        """Open provided event handle."""
        if eventhdl is not None:
            self.close_event()
            newevent = mkrace(self, eventhdl)
            newevent.loadconfig()
            self.curevent = newevent
            self.race_box.add(self.curevent.frame)
            self.menu_race_info.set_sensitive(True)
            self.menu_race_close.set_sensitive(True)
            self.menu_race_abort.set_sensitive(True)
            self.menu_race_startlist.set_sensitive(True)
            self.menu_race_result.set_sensitive(True)
            starters = eventhdl[u'star']
            if starters is not None and starters != u'':
                if u'auto' in starters:
                    spec = starters.lower().replace(u'auto', u'').strip()
                    self.curevent.autospec += spec
                    _log.info(u'Transferred autospec ' + repr(spec) +
                              u' to event ' + self.curevent.evno)
                else:
                    self.addstarters(
                        self.curevent,
                        eventhdl,  # xfer starters
                        strops.reformat_biblist(starters))
                eventhdl[u'star'] = u''
            self.menu_race_properties.set_sensitive(True)
            self.curevent.show()

    def addstarters(self, race, event, startlist):
        """Add each of the riders in startlist to the opened race."""
        starters = startlist.split()
        for st in starters:
            # check for category
            rlist = self.rdb.biblistfromcat(st, race.series)
            if len(rlist) > 0:
                for est in rlist:
                    race.addrider(est)
            else:
                race.addrider(st)

    def autoplace_riders(self, race, autospec=u'', infocol=None, final=False):
        """Fetch a flat list of places from the autospec."""
        # TODO: Consider an alternative since this is only used by ps
        places = {}
        for egroup in autospec.split(u';'):
            _log.debug(u'Autospec group: ' + repr(egroup))
            specvec = egroup.split(u':')
            if len(specvec) == 2:
                evno = specvec[0].strip()
                if evno not in self.autorecurse:
                    self.autorecurse.add(evno)
                    placeset = strops.placeset(specvec[1])
                    e = self.edb[evno]
                    if e is not None:
                        h = mkrace(self, e, False)
                        h.loadconfig()
                        isFinal = h.standingstr() == u'Result'
                        _log.debug(u'Event %r status: %r, final=%r', evno,
                                   h.standingstr(), isFinal)
                        if not final or isFinal:
                            for ri in h.result_gen():
                                if isinstance(ri[1],
                                              int) and ri[1] in placeset:
                                    rank = ri[1]
                                    if rank not in places:
                                        places[rank] = []
                                    places[rank].append(ri[0])
                        h.destroy()
                    else:
                        _log.warning(u'Autospec event number not found: ' +
                                     repr(evno))
                    self.autorecurse.remove(evno)
                else:
                    _log.debug(u'Ignoring loop in auto placelist: ' +
                               repr(evno))
            else:
                _log.warning(u'Ignoring erroneous autospec group: ' +
                             repr(egroup))
        ret = u''
        for place in sorted(places):
            ret += u' ' + u'-'.join(places[place])
        ## TODO: append to [] then join
        _log.debug(u'Place set: ' + ret)
        return ret

    def autostart_riders(self, race, autospec=u'', infocol=None):
        """Try to fetch the startlist from race result info."""
        # Dubious: infocol allows selection of seed info
        #          typical values:
        #                           1 -> timed event qualifiers
        #                           3 -> handicap
        # TODO: check default, maybe defer to None
        # TODO: IMPORTANT cache result gens for fast recall
        for egroup in autospec.split(u';'):
            _log.debug(u'Autospec group: ' + repr(egroup))
            specvec = egroup.split(u':')
            if len(specvec) == 2:
                evno = specvec[0].strip()
                if evno not in self.autorecurse:
                    self.autorecurse.add(evno)
                    placeset = strops.placeset(specvec[1])
                    e = self.edb[evno]
                    if e is not None:
                        evplacemap = {}
                        _log.debug('Loading places from event %r', evno)
                        ## load the place set map rank -> [[rider,seed],..]
                        h = mkrace(self, e, False)
                        h.loadconfig()
                        for ri in h.result_gen():
                            if isinstance(ri[1], int) and ri[1] in placeset:
                                rank = ri[1]
                                if rank not in evplacemap:
                                    evplacemap[rank] = []
                                seed = None
                                if infocol is not None and infocol < len(ri):
                                    seed = ri[infocol]
                                evplacemap[rank].append([ri[0], seed])
                                #_log.debug('Event %r add place=%r, rider=%r, info=%r',
                                #evno, rank, ri[0], seed)
                        h.destroy()
                        # maintain ordering of autospec
                        for p in placeset:
                            if p in evplacemap:
                                for ri in evplacemap[p]:
                                    #_log.debug(u'Adding rider: %r/%r', ri[0], ri[1])
                                    race.addrider(ri[0], ri[1])
                    else:
                        _log.warning(u'Autospec event number not found: ' +
                                     repr(evno))
                    self.autorecurse.remove(evno)
                else:
                    _log.debug(u'Ignoring loop in auto startlist: ' +
                               repr(evno))
            else:
                _log.warning(u'Ignoring erroneous autospec group: ' +
                             repr(egroup))

    def close_event(self):
        """Close the currently opened race."""
        if self.curevent is not None:
            self.menu_race_properties.set_sensitive(False)
            self.menu_race_info.set_sensitive(False)
            self.menu_race_close.set_sensitive(False)
            self.menu_race_abort.set_sensitive(False)
            self.menu_race_startlist.set_sensitive(False)
            self.menu_race_result.set_sensitive(False)
            # grab temporary handle to event to be closed
            delevent = self.curevent
            # invalidate curevent handle and then cleanup
            self.curevent = None
            delevent.hide()
            self.race_box.remove(delevent.frame)
            delevent.event[u'dirt'] = True  # mark event exportable
            delevent.destroy()

    def race_evno_change(self, old_no, new_no):
        """Handle a change in a race number."""
        if self.curevent is not None and self.curevent.evno == old_no:
            _log.warning(u'Ignoring change to open event: %r', old_no)
            return False
        newconf = self.event_configfile(new_no)
        if os.path.isfile(newconf):
            rnconf = newconf + u'.old'
            _log.debug(u'Backup existing config to %r', rnconf)
            os.rename(newconf, rnconf)
        oldconf = self.event_configfile(old_no)
        if os.path.isfile(oldconf):
            _log.debug(u'Rename config %r to %r', oldconf, newconf)
            os.rename(oldconf, newconf)
        _log.debug(u'Event %r changed to %r', old_no, new_no)
        return True

    ## Data menu callbacks.
    def menu_data_import_activate_cb(self, menuitem, data=None):
        """Re-load event and rider info from disk."""
        if not uiutil.questiondlg(self.window,
                                  u'Re-load event and rider data from disk?',
                                  u'Note: The current event will be closed.'):
            _log.debug(u'Re-load events & riders aborted')
            return False

        cureventno = None
        if self.curevent is not None:
            cureventno = self.curevent.evno
            self.close_event()

        self.rdb.clear()
        self.edb.clear()
        self.edb.load(u'events.csv')
        self.rdb.load(u'riders.csv')
        self.reload_riders()

        if cureventno and cureventno in self.edb:
            self.open_event(self.edb[cureventno])
        else:
            _log.warning(u'Running event was removed from the event list')

    def menu_data_result_activate_cb(self, menuitem, data=None):
        """Export final result."""
        try:
            self.finalresult()  # TODO: Call in sep thread
        except Exception as e:
            _log.error(u'Error writing result: ' + unicode(e))
            raise

    def finalresult(self):
        provisional = self.provisional  # may be overridden below
        sections = []
        lastsess = None
        for e in self.edb:
            r = mkrace(self, e, False)
            if e[u'resu']:  # include in result
                nsess = e[u'sess']
                if nsess != lastsess:
                    sections.append(
                        report.pagebreak(SESSBREAKTHRESH))  # force break
                    _log.debug(u'Break between events: ' + repr(e[u'evid']) +
                               u' with ' + repr(lastsess) + u' != ' +
                               repr(nsess))
                lastsess = nsess
                if r.evtype in [u'break', u'session']:
                    sec = report.section()
                    sec.heading = u' '.join([e[u'pref'], e[u'info']]).strip()
                    sec.subheading = u'\t'.join(
                        [strops.lapstring(e[u'laps']), e[u'dist'],
                         e[u'prog']]).strip()
                    sections.append(sec)
                else:
                    r.loadconfig()
                    if r.onestart:  # in progress or done...
                        rep = r.result_report()
                    else:
                        rep = r.startlist_report()
                    if len(rep) > 0:
                        sections.extend(rep)
            r.destroy()

        filebase = u'result'
        self.print_report(sections,
                          u'Results',
                          prov=provisional,
                          doprint=False,
                          exportfile=filebase.translate(strops.WEBFILE_UTRANS))

    def printprogram(self):
        r = report.report()
        subtitlestr = u'Program of Events'
        if self.subtitlestr:
            subtitlestr = self.subtitlestr + u' - ' + subtitlestr
        r.strings[u'title'] = self.titlestr
        r.strings[u'subtitle'] = subtitlestr
        r.strings[u'datestr'] = strops.promptstr(u'Date:', self.datestr)
        r.strings[u'commstr'] = strops.promptstr(u'Chief Commissaire:',
                                                 self.commstr)
        r.strings[u'orgstr'] = strops.promptstr(u'Organiser: ', self.orgstr)
        r.strings[u'docstr'] = u''  # What should go here?
        r.strings[u'diststr'] = self.locstr

        r.set_provisional(self.provisional)

        cursess = None
        for e in self.edb:
            if e[u'prin']:  # include this event in program
                if e[u'sess']:  # add harder break for new session
                    if cursess and cursess != e[u'sess']:
                        r.add_section(report.pagebreak(SESSBREAKTHRESH))
                    cursess = e[u'sess']
                h = mkrace(self, e, False)
                h.loadconfig()
                s = h.startlist_report(True)
                for sec in s:
                    r.add_section(sec)
                h.destroy()

        filebase = u'program'
        ofile = os.path.join(u'export', filebase + u'.pdf')
        with metarace.savefile(ofile) as f:
            r.output_pdf(f, docover=True)
            _log.info(u'Exported pdf program to %r', ofile)
        ofile = os.path.join(u'export', filebase + u'.html')
        with metarace.savefile(ofile) as f:
            r.output_html(f)
            _log.info(u'Exported html program to %r', ofile)
        ofile = os.path.join(u'export', filebase + u'.xls')
        with metarace.savefile(ofile) as f:
            r.output_xls(f)
            _log.info(u'Exported xls program to %r', ofile)
        ofile = os.path.join(u'export', filebase + u'.json')
        with metarace.savefile(ofile) as f:
            r.output_json(f)
            _log.info(u'Exported json program to %r', ofile)

    def menu_data_program_activate_cb(self, menuitem, data=None):
        """Export race program."""
        try:
            self.printprogram()  # TODO: call from sep thread
        except Exception as e:
            _log.error(u'Error writing report: ' + unicode(e))
            raise

    def menu_data_update_activate_cb(self, menuitem, data=None):
        """Update meet, session, event and riders in external database."""
        try:
            _log.info(u'Exporting data:')
            self.updateindex()  # TODO: push into sep thread
        except Exception as e:
            _log.error(u'Error exporting event data: ' + unicode(e))
            raise

    def updatenexprev(self):
        self.nextlinks = {}
        self.prevlinks = {}
        evlinks = {}
        evidx = []
        for eh in self.edb:
            if eh[u'inde'] or eh[u'resu']:  # include in index?
                evno = eh[u'evid']
                referno = None
                if eh[u'type'] not in [u'break', u'session']:
                    referno = evno
                if eh[u'refe']:  # overwrite ref no, even on specials
                    referno = eh[u'refe']
                linkfile = None
                if referno:
                    if referno not in evlinks:
                        evidx.append(referno)
                        evlinks[referno] = u'event_' + unicode(
                            referno).translate(strops.WEBFILE_UTRANS)
        prevno = None
        for evno in evidx:
            if prevno is not None:
                self.nextlinks[prevno] = evlinks[evno]
                self.prevlinks[evno] = evlinks[prevno]
            prevno = evno

    def updateindex(self):
        self.reload_riders()  # re-read rider list
        self.updatenexprev()  # re-compute next/prev link struct
        # check for printed program link
        # check for final result link
        # check for timing log link
        # build index of events report
        if self.mirrorpath:
            orep = report.report()
            orep.strings[u'title'] = self.titlestr
            orep.strings[u'subtitle'] = self.subtitlestr
            orep.strings[u'datestr'] = strops.promptstr(u'Date:', self.datestr)
            orep.strings[u'commstr'] = strops.promptstr(
                u'Chief Commissaire:', self.commstr)
            orep.strings[u'orgstr'] = strops.promptstr(u'Organiser: ',
                                                       self.orgstr)
            orep.strings[u'diststr'] = self.locstr
            orep.set_provisional(self.provisional)  # ! TODO
            orep.shortname = self.titlestr
            orep.indexlink = u'/'
            if self.provisional:
                orep.reportstatus = u'provisional'
            else:
                orep.reportstatus = u'final'

            pfilebase = u'program'
            pfile = os.path.join(u'export', pfilebase + u'.pdf')
            rfilebase = u'result'
            rfile = os.path.join(u'export', rfilebase + u'.pdf')

            lt = []
            lb = None
            if os.path.exists(rfile):
                lt = [u'pdf', u'xls']
                lb = os.path.join(self.linkbase, rfilebase)
            elif os.path.exists(pfile):
                lt = [u'pdf', u'xls']
                lb = os.path.join(self.linkbase, pfilebase)

            sec = report.event_index(u'eventindex')
            sec.heading = u'Index of Events'
            #sec.subheading = Date?
            for eh in self.edb:
                if eh[u'inde']:  # include in index?
                    evno = eh[u'evid']
                    if eh[u'type'] in [u'break', u'session']:
                        evno = None
                    referno = evno
                    if eh[u'refe']:  # overwrite ref no, even on specials
                        referno = eh[u'refe']
                    linkfile = None
                    if referno:
                        linkfile = u'event_' + unicode(referno).translate(
                            strops.WEBFILE_UTRANS)
                    descr = u' '.join([eh[u'pref'], eh[u'info']]).strip()
                    extra = None  # STATUS INFO -> progress?
                    if eh[u'evov'] is not None and eh[u'evov'] != u'':
                        evno = eh[u'evov'].strip()
                    sec.lines.append([evno, None, descr, extra, linkfile])
            orep.add_section(sec)
            basename = u'index'
            ofile = os.path.join(self.exportpath, basename + u'.html')
            with metarace.savefile(ofile) as f:
                orep.output_html(f, linkbase=lb, linktypes=lt)
            jbase = basename + u'.json'
            ofile = os.path.join(self.exportpath, jbase)
            with metarace.savefile(ofile) as f:
                orep.output_json(f)
            glib.idle_add(self.mirror_start)

    def mirror_completion(self, status, updates):
        """Send notifies for any changed files sent after export."""
        # NOTE: called in the mirror thread
        _log.debug(u'Mirror status: %r', status)
        if status == 0:
            pass
        else:
            _log.error(u'Mirror failed')
        return False

    def mirror_start(self, dirty=None):
        """Create a new mirror thread unless in progress."""
        if self.mirrorpath and self.mirror is None:
            self.mirror = export.mirror(callback=self.mirror_completion,
                                        callbackdata=dirty,
                                        localpath=os.path.join(
                                            self.exportpath, u''),
                                        remotepath=self.mirrorpath,
                                        mirrorcmd=self.mirrorcmd)
            self.mirror.start()
        return False  # for idle_add

    def menu_data_export_activate_cb(self, menuitem, data=None):
        """Export race data."""
        if not self.exportlock.acquire(False):
            _log.info(u'Export already in progress')
            return None  # allow only one entry
        if self.exporter is not None:
            _log.warning(u'Export in progress, re-run required')
            return False
        try:
            self.exporter = threading.Thread(target=self.__run_data_export)
            self.exporter.start()
            _log.debug(u'Created export worker %r: ', self.exporter)
        finally:
            self.exportlock.release()

    def __run_data_export(self):
        try:
            _log.debug('Exporting race info')
            self.updatenexprev()  # re-compute next/prev link struct

            # determine 'dirty' events 	## TODO !!
            dmap = {}
            dord = []
            for e in self.edb:  # note - this is the only traversal
                series = e[u'seri']
                #if series not in rmap:
                #rmap[series] = {}
                evno = e[u'evid']
                etype = e[u'type']
                prefix = e[u'pref']
                info = e[u'info']
                export = e[u'resu']
                key = evno  # no need to concat series, evno is unique
                dirty = e[u'dirt']
                if not dirty:  # check for any dependencies
                    for dev in e[u'depe'].split():
                        if dev in dmap:
                            dirty = True
                            break
                if dirty:
                    dord.append(key)  # maintains ordering
                    dmap[key] = [e, evno, etype, series, prefix, info, export]
            _log.debug(u'Marked ' + unicode(len(dord)) + u' events dirty')

            dirty = {}
            for k in dmap:  # only output dirty events
                # turn key into read-only event handle
                e = dmap[k][0]
                evno = dmap[k][1]
                etype = dmap[k][2]
                series = dmap[k][3]
                evstr = (dmap[k][4] + u' ' + dmap[k][5]).strip()
                doexport = dmap[k][6]
                e[u'dirt'] = False
                r = mkrace(self, e, False)
                r.loadconfig()

                # starters
                stcount = 0
                # this may not be required anymore - check
                startrep = r.startlist_report()  # trigger rider model reorder

                if self.mirrorpath and doexport:
                    orep = report.report()
                    orep.strings[u'title'] = self.titlestr
                    orep.strings[u'subtitle'] = evstr
                    #orep.strings[u'datestr'] = strops.promptstr(u'Date:',
                    #self.datestr)
                    # orep.strings[u'diststr'] = self.locstr
                    orep.strings[u'docstr'] = evstr
                    if etype in [u'classification']:
                        orep.strings[u'docstr'] += u' Classification'
                    orep.set_provisional(self.provisional)  # ! TODO
                    if self.provisional:
                        orep.reportstatus = u'provisional'
                    else:
                        orep.reportstatus = u'final'

                    # in page links
                    orep.shortname = evstr
                    orep.indexlink = u'./'  # url to program of events
                    if evno in self.prevlinks:
                        orep.prevlink = self.prevlinks[evno]
                    if evno in self.nextlinks:
                        orep.nextlink = self.nextlinks[evno]

                    # update files and trigger mirror
                    if r.onestart:  # output result
                        outsec = r.result_report()
                        for sec in outsec:
                            orep.add_section(sec)
                    else:  # startlist
                        outsec = r.startlist_report(u'startlist')
                        for sec in outsec:
                            orep.add_section(sec)
                    basename = u'event_' + unicode(evno).translate(
                        strops.WEBFILE_UTRANS)
                    ofile = os.path.join(self.exportpath, basename + u'.html')
                    with metarace.savefile(ofile) as f:
                        orep.output_html(f)
                    jbase = basename + u'.json'
                    ofile = os.path.join(self.exportpath, jbase)
                    with metarace.savefile(ofile) as f:
                        orep.output_json(f)
                r.destroy()
            glib.idle_add(self.mirror_start)
            _log.debug(u'Race info export')
        except Exception as e:
            _log.error(u'Error exporting results: %s', e)

    ## SCB menu callbacks
    def menu_scb_enable_toggled_cb(self, button, data=None):
        """Update scoreboard enable setting."""
        if button.get_active():
            self.scb.set_ignore(False)
            self.scb.setport(self.scbport)
            if self.scbwin is not None:
                self.scbwin.reset()
        else:
            self.scb.set_ignore(True)

    def menu_scb_clock_cb(self, menuitem, data=None):
        """Select timer scoreboard overlay."""
        self.gemini.clear()
        self.scbwin = None  # stop sending any new updates
        self.scb.clrall()  # force clear of current text page
        self.scb.sendmsg(unt4.OVERLAY_CLOCK)
        _log.debug(u'Show facility clock')

    def menu_scb_blank_cb(self, menuitem, data=None):
        """Select blank scoreboard overlay."""
        self.gemini.clear()
        self.scbwin = None
        self.scb.clrall()
        self.txt_announce(unt4.GENERAL_CLEARING)
        _log.debug(u'Blank scoreboard')

    def menu_scb_test_cb(self, menuitem, data=None):
        """Select test scoreboard overlay."""
        self.scbwin = None
        self.scbwin = scbwin.scbtest(self.scb)
        self.scbwin.reset()
        _log.debug(u'Scoreboard testpage')

    def menu_scb_connect_activate_cb(self, menuitem, data=None):
        """Force a reconnect to scoreboards."""
        self.scb.setport(self.scbport)
        self.announce.reconnect()
        _log.debug(u'Re-connect scoreboard')
        if self.gemport != u'':
            self.gemini.setport(self.gemport)

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

    def entry_set_now(self, button, entry=None):
        """Enter the current time in the provided entry."""
        entry.set_text(tod.now().timestr())
        entry.activate()

    def menu_timing_recalc(self, entry, ste, fte, nte):
        """Update the net time entry for the supplied start and finish."""
        st = tod.mktod(ste.get_text().decode('utf-8', 'replace'))
        ft = tod.mktod(fte.get_text().decode('utf-8', 'replace'))
        if st is not None and ft is not None:
            ste.set_text(st.timestr())
            fte.set_text(ft.timestr())
            nte.set_text((ft - st).timestr())

    def menu_timing_clear_activate_cb(self, menuitem, data=None):
        """Clear memory in attached timing devices."""
        self.main_timer.clrmem()
        _log.info(u'Clear timer memory')

    def menu_timing_dump_activate_cb(self, menuitem, data=None):
        """Request memory dump from attached timy."""
        self.main_timer.dumpall()
        _log.info(u'Dump timer memory')

    def menu_timing_reconnect_activate_cb(self, menuitem, data=None):
        """Reconnect timer and initialise."""
        self.main_timer.setport(self.main_port)
        if self.main_port:
            self.main_timer.sane()
        _log.info(u'Re-connect and initialise timer')

    ## Help menu callbacks
    def menu_help_about_cb(self, menuitem, data=None):
        """Display metarace about dialog."""
        metarace.about_dlg(self.window)

    ## Menu button callbacks
    def menu_clock_clicked_cb(self, button, data=None):
        """Handle click on menubar clock."""
        (line1, line2,
         line3) = strops.titlesplit(self.titlestr + u' ' + self.subtitlestr,
                                    self.scb.linelen)
        self.scbwin = scbwin.scbclock(self.scb,
                                      line1,
                                      line2,
                                      line3,
                                      locstr=self.locstr)
        self.scbwin.reset()

    ## Directory utilities
    def event_configfile(self, evno):
        """Return a config filename for the given event no."""
        return u'event_{}.json'.format(unicode(evno))

    ## Timer callbacks
    def menu_clock_timeout(self):
        """Update time of day on clock button."""

        if not self.running:
            return False
        else:
            tt = tod.now()
            self.clock_label.set_text(tt.meridiem())

            # check for completion in the export workers
            if self.mirror is not None:
                if not self.mirror.is_alive():  # replaces join() non-blocking
                    self.mirror = None
            if self.exporter is not None:
                if not self.exporter.is_alive(
                ):  # replaces join() non-blocking
                    _log.debug(u'Deleting complete export: %r', self.exporter)
                    self.exporter = None
                else:
                    _log.info(u'Incomplete export: %r', self.exporter)
        return True

    def timeout(self):
        """Update internal state and call into race timeout."""
        if not self.running:
            return False
        try:
            if self.curevent is not None:
                self.curevent.timeout()
            if self.scbwin is not None:
                self.scbwin.update()
        except Exception as e:
            _log.error(u'%s in timeout: %s', e.__class__.__name__, e)
        return True

    ## Timy utility methods.
    def timer_reprint(self, event=u'', trace=[]):
        self.main_timer.printer(True)  # turn on printer
        self.main_timer.printimp(False)  # suppress intermeds
        self.main_timer.printline(u'')
        self.main_timer.printline(u'')
        self.main_timer.printline(self.titlestr)
        self.main_timer.printline(self.subtitlestr)
        self.main_timer.printline(u'')
        if event:
            self.main_timer.printline(event)
            self.main_timer.printline(u'')
        for l in trace:
            self.main_timer.printline(l)
        self.main_timer.printline(u'')
        self.main_timer.printline(u'')
        self.main_timer.printer(False)

    def delayimp(self, dtime):
        """Set the impulse delay time."""
        self.main_timer.delaytime(dtime)

    def timer_log_event(self, ev=None):
        self.main_timer.printline(self.racenamecat(ev, slen=20, halign=u'l'))

    def timer_log_straight(self, bib, msg, tod, prec=4):
        """Print a tod log entry on the Timy receipt."""
        lstr = u'{0:3} {1: >5}:{2}'.format(bib[0:3], msg[0:5],
                                           tod.timestr(prec))
        self.main_timer.printline(lstr)

    def timer_log_msg(self, bib, msg):
        """Print the given msg entry on the Timy receipt."""
        lstr = u'{0:3} {1}'.format(bib[0:3], unicode(msg)[0:20])
        self.main_timer.printline(lstr)

    def event_string(self, evno):
        """Switch to suppress event no in delayed announce screens."""
        ret = u''
        if self.showevno:
            ret = u'Event ' + unicode(evno)
        else:
            ret = u' '.join([self.titlestr, self.subtitlestr]).strip()
        return ret

    def infoline(self, event):
        """Format event information for display on event info label."""
        evstr = event[u'pref'] + u' ' + event[u'info']
        if len(evstr) > 44:
            evstr = evstr[0:47] + u'\u2026'
        etype = event[u'type']
        return (u'Event {}: {} [{}]'.format(event[u'evid'], evstr, etype))

    def racenamecat(self, event, slen=None, tail=u'', halign=u'c'):
        """Concatentate race info for display on scoreboard header line."""
        if slen is None:
            slen = self.scb.linelen
        evno = u''
        srcev = event[u'evid']
        if self.showevno and event[u'type'] not in [u'break', u'session']:
            evno = u'Ev ' + srcev
        info = event[u'info']
        prefix = event[u'pref']
        ret = u' '.join([evno, prefix, info, tail]).strip()
        if len(ret) > slen + 1:
            ret = u' '.join([evno, info, tail]).strip()
            if len(ret) > slen + 1:
                ret = u' '.join([evno, tail]).strip()
        return strops.truncpad(ret, slen, align=halign)

    def racename(self, event):
        """Return a full event identifier string."""
        evno = u''
        if self.showevno and event[u'type'] not in [u'break', u'session']:
            evno = u'Event ' + event[u'evid']
        info = event[u'info']
        prefix = event[u'pref']
        return u' '.join([evno, prefix, info]).strip()

    ## Announcer methods
    def cmd_announce(self, command, msg):
        """Announce the supplied message to the command topic."""
        if self.anntopic:
            topic = u'/'.join((self.anntopic, command))
            self.announce.publish(msg, topic)

    def txt_announce(self, umsg):
        """Announce the unt4 message to the text-only DHI announcer."""
        if self.anntopic:
            topic = u'/'.join((self.anntopic, u'text'))
            self.announce.publish(umsg.pack(), topic)

    def txt_clear(self):
        """Clear the text announcer."""
        self.txt_announce(unt4.GENERAL_CLEARING)

    def txt_default(self):
        self.txt_announce(
            unt4.unt4(xx=1,
                      yy=0,
                      erl=True,
                      text=strops.truncpad(
                          u' '.join([
                              self.titlestr, self.subtitlestr, self.datestr
                          ]).strip(), ANNOUNCE_LINELEN - 2, u'c')))

    def txt_title(self, titlestr=u''):
        self.txt_announce(
            unt4.unt4(xx=1,
                      yy=0,
                      erl=True,
                      text=strops.truncpad(titlestr.strip(),
                                           ANNOUNCE_LINELEN - 2, u'c')))

    def txt_line(self, line, char=u'_'):
        self.txt_announce(
            unt4.unt4(xx=0, yy=line, text=char * ANNOUNCE_LINELEN))

    def txt_setline(self, line, msg):
        self.txt_announce(unt4.unt4(xx=0, yy=line, erl=True, text=msg))

    def txt_postxt(self, line, oft, msg):
        self.txt_announce(unt4.unt4(xx=oft, yy=line, text=msg))

    ## Window methods
    def set_title(self, extra=u''):
        """Update window title from meet properties."""
        self.window.set_title(
            u'trackmeet: ' +
            u' '.join([self.titlestr, self.subtitlestr]).strip())
        self.txt_default()

    def meet_destroy_cb(self, window, msg=u''):
        """Handle destroy signal and exit application."""
        rootlogger = logging.getLogger()
        rootlogger.removeHandler(self.sh)
        rootlogger.removeHandler(self.lh)
        self.window.hide()
        glib.idle_add(self.meet_destroy_handler)

    def meet_destroy_handler(self):
        lastevent = None
        if self.curevent is not None:
            lastevent = self.curevent.evno
            self.close_event()
        if self.started:
            self.saveconfig(lastevent)
            self.shutdown()
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
                if key in ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9']:
                    self.__timercb(tod.now(index=u'FAKE', chan=key))
                    return True
            if self.curevent is not None:
                return self.curevent.key_event(widget, event)
        return False

    def shutdown(self, msg=u''):
        """Cleanly shutdown threads and close application."""
        self.started = False
        self.gemini.exit(msg)
        self.announce.exit(msg)
        self.scb.exit(msg)
        self.main_timer.exit(msg)
        _log.info(u'Waiting for workers to exit')
        if self.exporter is not None:
            _log.debug(u'Result compiler')
            self.exporter.join()
            self.exporter = None
        if self.mirror is not None:
            _log.debug(u'Result export')
            self.mirror.join()
            self.mirror = None
        _log.debug(u'Gemini scoreboard')
        self.gemini.join()
        _log.debug(u'DHI scoreboard')
        self.scb.join()
        _log.debug(u'Telegraph/announce')
        self.announce.join()
        _log.debug(u'Main timer')
        self.main_timer.join()

    def __timercb(self, evt, data=None):
        if self.curevent is not None:
            glib.idle_add(self.curevent.timercb,
                          evt,
                          priority=glib.PRIORITY_HIGH)

    def __controlcb(self, topic=None, message=None):
        _log.debug(u'Unsupported control %r: %r', topic, message)

    def start(self):
        """Start the timer and scoreboard threads."""
        if not self.started:
            _log.debug(u'Meet startup')
            self.announce.start()
            self.scb.start()
            self.main_timer.setcb(self.__timercb)
            self.main_timer.start()
            self.gemini.start()
            self.started = True

    # Track meet functions
    def delayed_export(self):
        """Queue an export on idle add."""
        self.exportpending = True
        glib.idle_add(self.exportcb)

    def save_curevent(self):
        """Backup and save current event."""
        conf = self.event_configfile(self.curevent.event[u'evid'])
        backup = conf + u'.1'
        try:  # minimal effort backup (Posix only)
            if os.path.isfile(backup):
                os.remove(backup)
            if os.path.isfile(conf):
                _log.debug(u'Backing up %r to %r', conf, backup)
                os.link(conf, backup)
        except Exception as e:
            _log.debug(u'Backup of %r to %r failed: %s', conf, backup, e)
        self.curevent.saveconfig()
        self.curevent.event[u'dirt'] = True

    def exportcb(self):
        """Save current event and update race info in external db."""
        if not self.exportpending:
            return False  # probably doubled up
        self.exportpending = False
        if self.curevent is not None and self.curevent.winopen:
            self.save_curevent()
        self.menu_data_export_activate_cb(None)
        return False  # for idle add

    def saveconfig(self, lastevent=None):
        """Save current meet data to disk."""
        cw = jsonconfig.config()
        cw.add_section(u'trackmeet')
        cw.set(u'trackmeet', u'id', TRACKMEET_ID)
        if self.curevent is not None and self.curevent.winopen:
            self.save_curevent()
            cw.set(u'trackmeet', u'curevent', self.curevent.evno)
        elif lastevent is not None:
            cw.set(u'trackmeet', u'curevent', lastevent)
        cw.set(u'trackmeet', u'timerprint', self.timerprint)
        cw.set(u'trackmeet', u'maintimer', self.main_port)
        cw.set(u'trackmeet', u'gemini', self.gemport)
        cw.set(u'trackmeet', u'racetimer', u'main')
        cw.set(u'trackmeet', u'scbport', self.scbport)
        cw.set(u'trackmeet', u'anntopic', self.anntopic)
        cw.set(u'trackmeet', u'title', self.titlestr)
        cw.set(u'trackmeet', u'subtitle', self.subtitlestr)
        cw.set(u'trackmeet', u'date', self.datestr)
        cw.set(u'trackmeet', u'location', self.locstr)
        cw.set(u'trackmeet', u'organiser', self.orgstr)
        cw.set(u'trackmeet', u'commissaire', self.commstr)
        cw.set(u'trackmeet', u'mirrorpath', self.mirrorpath)
        cw.set(u'trackmeet', u'mirrorcmd', self.mirrorcmd)
        cw.set(u'trackmeet', u'linkbase', self.linkbase)
        cw.set(u'trackmeet', u'clubmode', self.clubmode)
        cw.set(u'trackmeet', u'showevno', self.showevno)
        cw.set(u'trackmeet', u'provisional', self.provisional)
        cw.set(u'trackmeet', u'communiques', self.communiques)
        cw.set(u'trackmeet', u'commalloc', self.commalloc)  # map
        cw.set(u'trackmeet', u'tracklen_n',
               unicode(self.tracklen_n))  # poss val?
        cw.set(u'trackmeet', u'tracklen_d', unicode(self.tracklen_d))
        cw.set(u'trackmeet', u'docindex', unicode(self.docindex))
        with metarace.savefile(CONFIGFILE) as f:
            cw.write(f)
        self.rdb.save(u'riders.csv')
        self.edb.save(u'events.csv')
        _log.info(u'Meet configuration saved')

    def reload_riders(self):
        # make a prelim mapped rider struct
        self.ridermap = {}
        for s in self.rdb.listseries():
            self.ridermap[s] = self.rdb.mkridermap(s)

    def loadconfig(self):
        """Load meet config from disk."""
        cr = jsonconfig.config({
            u'trackmeet': {
                u'maintimer': u'',
                u'timerprint': False,
                u'racetimer': u'main',
                u'scbport': u'',
                u'anntopic': None,
                u'showevno': True,
                u'resultnos': True,
                u'clubmode': True,
                u'tracklen_n': 250,
                u'tracklen_d': 1,
                u'docindex': u'0',
                u'gemini': u'',
                u'dbhost': u'',
                u'title': u'',
                u'subtitle': u'',
                u'date': u'',
                u'location': u'',
                u'organiser': u'',
                u'commissaire': u'',
                u'curevent': u'',
                u'mirrorcmd': u'',
                u'mirrorpath': u'',
                u'linkbase': u'.',
                u'provisional': False,
                u'communiques': False,
                u'commalloc': {},
                u'id': u''
            }
        })
        cr.add_section(u'trackmeet')
        cr.merge(metarace.sysconf, u'trackmeet')
        _log.debug(u'Load system meet defaults')

        # re-set main log file
        _log.debug(u'Adding meet logfile handler %r', LOGFILE)
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
        try:
            with open(CONFIGFILE, 'rb') as f:
                cr.read(f)
            _log.debug(u'Read meet config from %r', CONFIGFILE)
        except Exception as e:
            _log.error(u'Unable to read meet config: %s', e)

        # set main timer port (only main timer now)
        nport = cr.get(u'trackmeet', u'maintimer')
        if nport != self.main_port:
            self.main_port = nport
            self.main_timer.setport(nport)
            self.main_timer.sane()

        # add gemini board if defined
        self.gemport = cr.get(u'trackmeet', u'gemini')
        if self.gemport != u'':
            self.gemini.setport(self.gemport)

        # flag timer print in time-trial mode
        self.timerprint = strops.confopt_bool(
            cr.get(u'trackmeet', u'timerprint'))

        # reset announcer topic
        self.anntopic = cr.get(u'trackmeet', u'anntopic')
        if self.anntopic:
            self.announce.subscribe(u'/'.join(
                (self.anntopic, u'control', u'#')))

        # connect DHI scoreboard
        nport = cr.get(u'trackmeet', u'scbport')
        if self.scbport != nport:
            self.scbport = nport
            self.scb.setport(nport)

        # set meet meta infos, and then copy into text entries
        self.titlestr = cr.get(u'trackmeet', u'title')
        self.subtitlestr = cr.get(u'trackmeet', u'subtitle')
        self.datestr = cr.get(u'trackmeet', u'date')
        self.locstr = cr.get(u'trackmeet', u'location')
        self.orgstr = cr.get(u'trackmeet', u'organiser')
        self.commstr = cr.get(u'trackmeet', u'commissaire')
        self.mirrorpath = cr.get(u'trackmeet', u'mirrorpath')
        self.mirrorcmd = cr.get(u'trackmeet', u'mirrorcmd')
        self.linkbase = cr.get(u'trackmeet', u'linkbase')
        self.set_title()

        # result options (bool)
        self.clubmode = strops.confopt_bool(cr.get(u'trackmeet', u'clubmode'))
        self.showevno = strops.confopt_bool(cr.get(u'trackmeet', u'showevno'))
        self.provisional = strops.confopt_bool(
            cr.get(u'trackmeet', u'provisional'))
        self.communiques = strops.confopt_bool(
            cr.get(u'trackmeet', u'communiques'))
        # communique allocations -> fixed once only
        self.commalloc = cr.get(u'trackmeet', u'commalloc')

        # track length
        n = strops.confopt_posint(cr.get(u'trackmeet', u'tracklen_n'), 0)
        d = strops.confopt_posint(cr.get(u'trackmeet', u'tracklen_d'), 0)
        setlen = False
        if n > 0 and n < 5500 and d > 0 and d < 10:  # sanity check
            self.tracklen_n = n
            self.tracklen_d = d
            setlen = True
            _log.debug(u'Track length updated to %r/%r', n, d)
        if not setlen:
            _log.warning(u'Ignoring invalid track length %r/%r default used',
                         n, d)

        # document id
        self.docindex = strops.confopt_posint(
            cr.get(u'trackmeet', u'docindex'), 0)
        self.rdb.clear()
        self.edb.clear()
        self.edb.load(u'events.csv')
        self.rdb.load(u'riders.csv')
        self.reload_riders()

        cureventno = cr.get(u'trackmeet', u'curevent')
        if cureventno and cureventno in self.edb:
            self.open_event(self.edb[cureventno])

        # make sure export path exists
        if not os.path.exists(self.exportpath):
            os.mkdir(self.exportpath)
            _log.info(u'Created export path: %r', self.exportpath)

        # check and warn of config mismatch
        cid = cr.get(u'trackmeet', u'id')
        if cid != TRACKMEET_ID:
            _log.warning(u'Meet config mismatch: %r != %r', cid, TRACKMEET_ID)

    def newgetrider(self, bib, series=u''):
        ret = None
        if series in self.ridermap and bib in self.ridermap[series]:
            ret = self.ridermap[series][bib]
        return ret

    def rider_edit(self, bib, series=u'', col=-1, value=u''):
        dbr = self.rdb.getrider(bib, series)
        if dbr is None:
            dbr = self.rdb.addempty(bib, series)
        if col == riderdb.COL_FIRST:
            self.rdb.editrider(ref=dbr, first=value)
        elif col == riderdb.COL_LAST:
            self.rdb.editrider(ref=dbr, last=value)
        elif col == riderdb.COL_CLUB:
            self.rdb.editrider(ref=dbr, club=value)
        else:
            _log.debug(u'Attempt to edit unsupported rider column: %r', col)
        self.reload_riders()

    def get_clubmode(self):
        return self.clubmode

    def get_distance(self, count=None, units=u'metres'):
        """Convert race distance units to metres."""
        ret = None
        if count is not None:
            try:
                if units in [u'metres', u'meters']:
                    ret = int(count)
                elif units == u'laps':
                    ret = self.tracklen_n * int(count)
                    if self.tracklen_d != 1 and self.tracklen_d > 0:
                        ret //= self.tracklen_d
                _log.debug(u'get_distance: %r %r -> %dm', count, units, ret)
            except (ValueError, TypeError, ArithmeticError) as v:
                _log.warning(u'Error computing race distance: %s', v)
        return ret

    def __init__(self):
        """Meet constructor."""
        self.loghandler = None  # set in loadconfig to meet dir
        self.exportpath = EXPORTPATH
        self.titlestr = u''
        self.subtitlestr = u''
        self.datestr = u''
        self.locstr = u''
        self.orgstr = u''
        self.commstr = u''
        self.clubmode = True
        self.showevno = True
        self.provisional = False
        self.communiques = False
        self.nextlinks = {}
        self.prevlinks = {}
        self.commalloc = {}
        self.timerport = None
        self.tracklen_n = 250  # numerator
        self.tracklen_d = 1  # denominator
        self.docindex = 0  # used for session number
        self.exportpending = False
        self.mirrorpath = u''  # default mirror path
        self.mirrorcmd = u''  # default mirror command
        self.linkbase = u'.'

        # printer preferences
        paper = gtk.paper_size_new_custom(u'metarace-full', u'A4 for reports',
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
        _log.debug(u'Adding hardware connections')
        self.scb = sender.mksender()
        self.announce = telegraph.telegraph()
        self.announce.setcb(self.__controlcb)
        self.scbport = u''
        self.anntopic = None
        self.timerprint = False  # enable timer printer?
        self.main_timer = timy.timy()
        self.main_port = u''
        self.gemini = gemini.gemini()
        self.gemport = u''
        self.mirror = None  # file mirror thread
        self.exporter = None  # export worker thread
        self.exportlock = threading.Lock()  # one only exporter

        uifile = os.path.join(metarace.UI_PATH, u'trackmeet.ui')
        _log.debug(u'Building user interface from %r', uifile)
        b = gtk.Builder()
        b.add_from_file(uifile)
        self.window = b.get_object(u'meet')
        self.window.connect(u'key-press-event', self.key_event)
        self.clock = b.get_object(u'menu_clock')
        self.clock_label = b.get_object(u'menu_clock_label')
        self.status = b.get_object(u'status')
        self.log_buffer = b.get_object(u'log_buffer')
        self.log_view = b.get_object(u'log_view')
        self.log_view.modify_font(uiutil.LOGVIEWFONT)
        self.log_scroll = b.get_object(u'log_box').get_vadjustment()
        self.context = self.status.get_context_id(u'metarace meet')
        self.menubut_main = b.get_object(u'menu_timing_main')
        self.menubut_backup = b.get_object(u'menu_timing_backup')
        self.menu_race_info = b.get_object(u'menu_race_info')
        self.menu_race_properties = b.get_object(u'menu_race_properties')
        self.menu_race_close = b.get_object(u'menu_race_close')
        self.menu_race_abort = b.get_object(u'menu_race_abort')
        self.menu_race_startlist = b.get_object(u'menu_race_startlist')
        self.menu_race_result = b.get_object(u'menu_race_result')
        self.race_box = b.get_object(u'race_box')
        self.new_race_pop = b.get_object(u'menu_race_new_types')
        b.connect_signals(self)

        # run state
        self.scbwin = None
        self.running = True
        self.started = False
        self.curevent = None
        self.autorecurse = set()

        # connect UI log handlers
        _log.debug(u'Connecting interface log handlers')
        rootlogger = logging.getLogger()
        f = logging.Formatter(metarace.LOGFORMAT)
        self.sh = loghandler.statusHandler(self.status, self.context)
        self.sh.setFormatter(f)
        self.sh.setLevel(logging.WARNING)  # show warn+ on status bar
        rootlogger.addHandler(self.sh)
        self.lh = loghandler.textViewHandler(self.log_buffer, self.log_view,
                                             self.log_scroll)
        self.lh.setFormatter(f)
        self.lh.setLevel(logging.INFO)  # show info+ in text view
        rootlogger.addHandler(self.lh)

        # get rider db and pack into scrolled pane
        _log.debug(u'Add riderdb and eventdb')
        self.rdb = riderdb.riderdb()
        self.ridermap = {}
        b.get_object(u'rider_box').add(self.rdb.mkview(ucicode=True,
                                                       note=True))
        # get event db and pack into scrolled pane
        self.edb = eventdb.eventdb()
        self.edb.set_startlist_cb(self.eventdb_cb, u'startlist')
        self.edb.set_result_cb(self.eventdb_cb, u'result')
        self.edb.set_program_cb(self.eventdb_cb, u'program')
        b.get_object(u'event_box').add(self.edb.mkview())
        self.edb.set_evno_change_cb(self.race_evno_change)
        # connect each of the race menu types if present in builder
        for etype in self.edb.racetypes:
            lookup = u'mkrace_' + etype.replace(u' ', u'_')
            mi = b.get_object(lookup)
            if mi is not None:
                mi.connect(u'activate', self.menu_race_make_activate_cb, etype)

        # start timers
        _log.debug(u'Starting meet timers')
        glib.timeout_add_seconds(1, self.menu_clock_timeout)
        glib.timeout_add(50, self.timeout)


def main():
    """Run the track meet application as a console script."""
    # attach a console log handler to the root logger then run the ui
    ch = logging.StreamHandler()
    ch.setLevel(metarace.LOGLEVEL)
    fh = logging.Formatter(metarace.LOGFORMAT)
    ch.setFormatter(fh)
    logging.getLogger().addHandler(ch)

    metarace.init(withgtk=True)
    configpath = metarace.DATA_PATH
    if len(sys.argv) > 2:
        _log.error(u'Usage: trackmeet [configdir]')
        sys.exit(1)
    elif len(sys.argv) == 2:
        configpath = sys.argv[1]
    configpath = metarace.config_path(configpath)
    if configpath is None:
        _log.error(u'Unable to open meet config %r', sys.argv[1])
        sys.exit(1)
    app = runapp(configpath)
    try:
        metarace.mainloop()
    except:
        app.shutdown(u'Exception from main loop')
        raise
    return 0


def runapp(configpath):
    """Create the trackmeet object, start in configpath and return a handle."""
    lf = metarace.lockpath(configpath)
    if lf is None:
        _log.error(u'Unable to lock meet config, already in use')
        sys.exit(1)
    _log.debug(u'Entering meet folder %r', configpath)
    os.chdir(configpath)
    app = trackmeet()
    app.loadconfig()
    app.window.show()
    app.start()
    return app


if __name__ == u'__main__':
    main()
