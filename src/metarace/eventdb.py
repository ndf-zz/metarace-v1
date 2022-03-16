"""Event 'database' and utilities."""

import gtk
import gobject
import glib
import logging
import threading
import os

import metarace
from metarace import uiutil
from metarace import strops
from metarace import ucsv  # replace with csv in >= 3

LOG = logging.getLogger(u'metarace.eventdb')
LOG.setLevel(logging.DEBUG)

# Note: These are for the trackmeet module, roadmeet re-defines race types
defracetypes = [
    u'sprint',
    u'keirin',  # sprint types
    u'flying 200',
    u'flying lap',  # flying TTs
    u'indiv tt',
    u'indiv pursuit',  # timed events
    u'pursuit race',  # indiv tt with places
    u'points',
    u'madison',
    u'omnium',  # point score types
    u'classification',  # final classification
    u'hour',  # UCI hour record
    u'competition',
    u'break',  # 
    u'break',  # break/non-race
    u'sprint round',
    u'sprint final',  # special 2-up sprint models
    u'scratch',
    u'motorpace',
    u'handicap',
    u'elimination',
    u'race',  # generic races
]

# View-Model column constants
COL_EVNO = 0
COL_INFO = 1
COL_TYPE = 2

# default event values (if not empty string)
EVENT_DEFAULTS = {
    u'evid': None,  # empty not allowed
    u'resu': True,
    u'inde': False,
    u'prin': False,
    u'dirt': False,
    u'plac': None,
    u'laps': None,
}

# event column heading and key mappings
EVENT_COLUMNS = {
    u'evid': u"EvID",
    u'refe': u"Reference Number",
    u'pref': u"Prefix",
    u'info': u"Information",
    u'seri': u"Series",
    u'type': u"Type Handler",
    u'star': u"Starters",
    u'depe': u"Depends On Events",
    u'resu': u"Result Include?",
    u'inde': u"Index Include?",
    u'prin': u"Printed Program Include?",
    u'plac': u"Placeholder Count",
    u'sess': u"Session",
    u'laps': u"Laps Count",
    u'dist': u"Distance String",
    u'prog': u"Progression Rules String",
    u'reco': u"Record String",
    u'dirt': u"Dirty?",
    u'evov': u"EVOverride"
}
# for any non-strings, types as listed
EVENT_COLUMN_CONVERTERS = {
    u'resu': strops.confopt_bool,
    u'inde': strops.confopt_bool,
    u'prin': strops.confopt_bool,
    u'dirt': strops.confopt_bool,
    u'plac': strops.confopt_posint,
    u'laps': strops.confopt_posint,
}

DEFAULT_COLUMN_ORDER = [
    u'evid', u'refe', u'pref', u'info', u'seri', u'type', u'star', u'depe',
    u'resu', u'inde', u'prin', u'plac', u'sess', u'laps', u'dist', u'prog',
    u'evov', u'reco', u'dirt'
]


def colkey(colstr=u''):
    return colstr[0:4].lower()


def get_header(coldump=DEFAULT_COLUMN_ORDER):
    """Return a header row."""
    return [EVENT_COLUMNS[c] for c in coldump]


class event(object):
    """Emulate dict, but use default values."""

    def get_row(self, coldump=DEFAULT_COLUMN_ORDER):
        """Return a row ready to export."""
        return [unicode(self[c]) for c in coldump]

    def event_info(self):
        """Return a concatenated and stripped event information string."""
        return u' '.join([self[u'pref'], self[u'info']]).strip()

    def event_type(self):
        """Return event type string."""
        return self[u'type'].capitalize()

    def set_notify(self, callback=None):
        """Set or clear the notify callback for the event."""
        if callback is not None:
            self.__notify = callback
        else:
            self.__notify = self.__def_notify

    def get_value(self, key):
        """Alternate value fetch."""
        return self.__getitem__(key)

    def set_value(self, key, value):
        """Update a value without triggering notify."""
        key = colkey(key)
        self.__store[key] = value

    def notify(self):
        """Forced notify."""
        self.__notify(self.__store[u'evid'])

    def __init__(self, evid=None, notify=None, cols={}):
        self.__store = dict(cols)
        self.__notify = self.__def_notify
        if u'evid' not in self.__store:
            self.__store[u'evid'] = evid
        if notify is not None:
            self.__notify = notify

    def __len__(self):
        return len(self.__store)

    def __getitem__(self, key):
        """Use a default value id, but don't save it."""
        key = colkey(key)
        if key in self.__store:
            return self.__store[key]
        elif key in EVENT_DEFAULTS:
            return EVENT_DEFAULTS[key]
        else:
            return u''

    def __setitem__(self, key, value):
        key = colkey(key)
        self.__store[key] = value
        self.__notify(self.__store[u'evid'])

    def __delitem__(self, key):
        key = colkey(key)
        del (self.__store[key])
        self.__notify(self.__store[u'evid'])

    def __iter__(self):
        return self.__store.iterkeys()

    def iterkeys(self):
        return self.__store.iterkeys()

    def __contains__(self, item):
        key = colkey(item)
        return key in self.__store

    def __def_notify(self, data=None):
        pass


class eventdb(object):
    """Event database."""

    def add_empty(self, gotorow=False, evno=None):
        """Add a new empty row to the event model."""
        if evno is None:
            evno = self.nextevno()
        nev = event(evid=evno, notify=self.__notify)
        self.__store[evno] = nev
        self.__index.append(evno)
        self.__notify(None)  # NOTE: Triggers delete cb on each row!!
        return nev

    def clear(self):
        """Clear event model."""
        self.__index = []
        self.__store = {}
        self.__notify(None)
        LOG.debug(u'Event model cleared')

    def change_evno(self, oldevent, newevent):
        """Attempt to change the event id."""
        if oldevent not in self:
            LOG.error(u'Unknown old event %r', oldevent)
            return False

        if newevent in self:
            LOG.error(u'New event %r already exists', newevent)
            return False

        oktochg = True
        if self.__evno_change_cb is not None:
            oktochg = self.__evno_change_cb(oldevent, newevent)
        if oktochg:
            ref = self.__store[oldevent]
            ref.set_value(u'evid', newevent)
            cnt = 0
            for j in self.__index:
                if j == oldevent:
                    break
                cnt += 1
            if cnt < len(self.__index):
                self.__index[cnt] = newevent
            del (self.__store[oldevent])
            self.__store[newevent] = ref
            LOG.warning(u'Updated event %r to %r', oldevent, newevent)
            return True
        return False

    def add_event(self, newevent):
        """Append newevent to model."""
        evno = newevent[u'evid']
        if evno not in self.__index:
            newevent.set_notify(self.__notify)
            self.__store[evno] = newevent
            self.__index.append(evno)
        else:
            LOG.warning(u'Duplicate event id %r ignored', evno)

    def __loadrow(self, r, colspec):
        nev = event()
        for i in range(0, len(colspec)):
            if len(r) > i:  # column data in row
                val = r[i].translate(strops.PRINT_UTRANS)
                key = colspec[i]
                if key in EVENT_COLUMN_CONVERTERS:
                    val = EVENT_COLUMN_CONVERTERS[key](val)
                nev[key] = val
        if u'evid' not in colspec:  # assigned None in init
            evno = self.nextevno()
            LOG.info(u'Event without id assigned %r', evno)
            nev[u'evid'] = evno
        self.add_event(nev)

    def load(self, csvfile=None):
        """Load events from supplied CSV file."""
        if not os.path.isfile(csvfile):
            LOG.debug(u'Creating new event database')
            return
        LOG.debug(u'Loading events from %r', csvfile)
        with open(csvfile, 'rb') as f:
            cr = ucsv.UnicodeReader(f)
            incols = None  # no header
            for r in cr:
                if len(r) > 0:  # got a data row
                    if incols is not None:  # already got col header
                        self.__loadrow(r, incols)
                    else:
                        # determine input column structure
                        if colkey(r[0]) in EVENT_COLUMNS:
                            incols = []
                            for col in r:
                                incols.append(colkey(col))
                        else:
                            incols = DEFAULT_COLUMN_ORDER  # assume full
                            self.__loadrow(r, incols)
        self.__notify(None)

    def save(self, csvfile=None):
        """Save current model content to supplied CSV file."""
        if self.view is not None:  # reorder according to view
            if len(self.__index) == len(self.__viewmodel):
                newindex = []
                for r in self.__viewmodel:
                    newindex.append(r[COL_EVNO])
                self.__index = newindex  # but leave reordering in model!
            else:
                LOG.info(u'View out of sync, dumping whole index')

        if len(self.__index) != len(self.__store):
            LOG.error(u'Index out of sync with model, dumping whole model')
            self.__index = [a for a in self.__store]

        LOG.debug(u'Saving events to %r', csvfile)
        with metarace.savefile(csvfile) as f:
            cr = ucsv.UnicodeWriter(f)
            cr.writerow(get_header(self.include_cols))
            for r in self:
                cr.writerow(r.get_row())

    def nextevno(self):
        """Try and return a new unique event number string."""
        lmax = 1
        for r in self.__index:
            if r.isdigit() and int(r) >= lmax:
                lmax = int(r) + 1
        return unicode(lmax)

    def mkpopup(self):
        """Create and return the eventdb popup menu."""
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'eventdb_context.ui'))
        self.__viewcontext = b.get_object('eventdb_context')
        self.__viewcontext_edit = b.get_object('edit')
        b.connect_signals(self)

    def popup_delete_cb(self, menuitem, data=None):
        """Delete selected row."""
        sel = self.view.get_selection()
        cnt = sel.count_selected_rows()
        # check for one selected
        if cnt == 0:
            LOG.debug(u'No rows selected for delete')
            return False

        # convert model iters into a list of event numbers
        (model, iters) = sel.get_selected_rows()
        elist = [model[i][0] for i in iters]

        msg = u'Delete selected events?'
        if sel.count_selected_rows() == 1:
            evt = self[elist[0]]
            sep = u''
            ifstr = evt.event_info()
            if ifstr:
                sep = u': '
            evno = evt[u'evid']
            msg = (u'Delete event ' + evno + sep + ifstr + u'?')

        dlg = gtk.MessageDialog(self.__viewparent,
                                gtk.DIALOG_DESTROY_WITH_PARENT,
                                gtk.MESSAGE_QUESTION, gtk.BUTTONS_YES_NO, msg)
        ret = dlg.run()
        dlg.destroy()
        if ret == gtk.RESPONSE_YES:
            for evt in elist:
                LOG.debug(u'Deleting event %r', evt)
                del (self[evt])
            self.__notify(None)

    def set_result_cb(self, cb=None, data=None):
        """Set the 'result' callback function."""
        self.__popup_result_cb = cb
        self.__popup_result_data = data

    def set_startlist_cb(self, cb=None, data=None):
        """Set the 'startlist' callback function."""
        self.__popup_startlist_cb = cb
        self.__popup_startlist_data = data

    def set_program_cb(self, cb=None, data=None):
        """Set the 'program' callback function."""
        self.__popup_program_cb = cb
        self.__popup_program_data = data

    def popup_result_cb(self, menuitem, data=None):
        """Print event results."""
        if self.__popup_result_cb:
            # call the provided callback with a list of event nos
            sel = self.view.get_selection()
            cnt = sel.count_selected_rows()
            # check for one selected
            if cnt == 0:
                LOG.debug(u'No rows selected for result')
                return False

            # convert model iters into a list of event numbers
            (model, iters) = sel.get_selected_rows()
            elist = [model[i][0] for i in iters]

            # queue callback in main loop
            glib.idle_add(self.__popup_result_cb, elist,
                          self.__popup_result_data)

    def popup_startlist_cb(self, menuitem, data=None):
        """Print event startlists."""
        if self.__popup_startlist_cb:
            # call the provided callback with a list of event nos
            sel = self.view.get_selection()
            cnt = sel.count_selected_rows()
            # check for one selected
            if cnt == 0:
                LOG.debug(u'No rows selected for startlist')
                return False

            # convert model iters into a list of event numbers
            (model, iters) = sel.get_selected_rows()
            elist = [model[i][0] for i in iters]

            # queue callback in main loop
            glib.idle_add(self.__popup_startlist_cb, elist,
                          self.__popup_startlist_data)

    def popup_program_cb(self, menuitem, data=None):
        """Print program of events."""
        if self.__popup_startlist_cb:
            # call the provided callback with a list of event nos
            sel = self.view.get_selection()
            cnt = sel.count_selected_rows()
            # check for one selected
            if cnt == 0:
                LOG.debug(u'No rows selected for program')
                return False

            # convert model iters into a list of event numbers
            (model, iters) = sel.get_selected_rows()
            elist = [model[i][0] for i in iters]

            # queue callback in main loop
            glib.idle_add(self.__popup_program_cb, elist,
                          self.__popup_program_data)

    def popup_insert_cb(self, menuitem, data=None):
        """Add new empty row."""
        self.add_empty()
        # Select new row?

    def set_evno_change_cb(self, cb, data=None):
        """Set the event no change callback."""
        self.__evno_change_cb = cb

    def popup_edit_cb(self, menuitem, data=None):
        """Edit event extended attributes."""
        evno = None
        ref = None
        model, plist = self.view.get_selection().get_selected_rows()
        if len(plist) > 0:
            evno = self.__viewmodel[plist[0]][COL_EVNO]
            if evno in self:
                ref = self[evno]
            else:
                LOG.error(u'Event %r in view not found in model', evno)
        if ref is None:
            LOG.error(u'No event selected for edit')
            return False

        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'generic_props.ui'))
        dlg = b.get_object('properties')
        dlg.set_transient_for(self.__viewparent)
        tbl = b.get_object('props_table')
        rows = len(DEFAULT_COLUMN_ORDER)
        tbl.resize(rows, 3)
        entmap = {}
        rof = 0
        for i in DEFAULT_COLUMN_ORDER:
            prompt = EVENT_COLUMNS[i]
            ne = uiutil.mktextentry(prompt, rof, tbl)
            ne.set_text(unicode(ref[i]))
            #if i == u'evid':
            #ne.set_sensitive(False)
            entmap[i] = ne
            rof += 1
        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            LOG.info(u'Updating event')
            do_notify = False

            # check for event id change
            newevno = entmap[u'evid'].get_text().decode('utf-8',
                                                        'ignore').strip()
            if newevno != evno:
                do_notify = self.change_evno(evno, newevno)

            for i in DEFAULT_COLUMN_ORDER:
                if i == u'evid':
                    continue  # ignore change for now
                nv = entmap[i].get_text().decode('utf-8', 'ignore')
                if i in EVENT_COLUMN_CONVERTERS:
                    nv = EVENT_COLUMN_CONVERTERS[i](nv)
                ov = ref[i]
                if ov != nv:
                    ref.set_value(i, nv)  # update without notify
                    do_notify = True
            if do_notify:
                ref.notify()  # notify here if required
        else:
            LOG.info(u'Edit event cancelled')
        dlg.destroy()

    def __view_button_cb(self, treeview, event):
        if event.button == 3:
            x = int(event.x)
            y = int(event.y)
            time = event.time
            pthinfo = treeview.get_path_at_pos(x, y)
            if pthinfo is not None:
                path, col, cellx, celly = pthinfo
                sel = treeview.get_selection()
                if sel.path_is_selected(path):
                    # pressed path is already in current selection
                    pass
                else:
                    treeview.grab_focus()
                    treeview.set_cursor(path, col, 0)
                if sel.count_selected_rows() > 1:
                    # prepare context for multiple select
                    self.__viewcontext_edit.set_sensitive(False)
                else:
                    # prepare context for single select
                    self.__viewcontext_edit.set_sensitive(True)
            else:
                # assume right click on no row
                self.__viewcontext_edit.set_sensitive(False)
            self.__viewcontext.popup(None, None, None, event.button, time)
            return True

    def mkview(self, parentwin=None):
        """Create and return view object for the model."""
        if self.__viewmodel is None:
            self.__viewmodel = gtk.ListStore(gobject.TYPE_STRING,
                                             gobject.TYPE_STRING,
                                             gobject.TYPE_STRING)
        self.__notify(None)  # trigger reload
        if self.view is not None:
            return self.view
        self.__viewparent = parentwin
        v = gtk.TreeView(self.__viewmodel)
        #v.set_reorderable(True)
        v.set_enable_search(False)
        v.set_rules_hint(True)
        v.get_selection().set_mode(gtk.SELECTION_MULTIPLE)
        v.show()
        uiutil.mkviewcoltxt(v, 'No.', COL_EVNO)
        uiutil.mkviewcoltxt(v, 'Info', COL_INFO, expand=True)
        uiutil.mkviewcoltxt(v, 'Type', COL_TYPE, expand=True)
        self.view = v
        self.view.connect('button-press-event', self.__view_button_cb)
        self.mkpopup()
        return self.view

    def getselected(self):
        """Return event for the currently selected row, or None."""
        ref = None
        if self.view is not None:
            model, plist = self.view.get_selection().get_selected_rows()
            if len(plist) > 0:
                evno = self.__viewmodel[plist[0]][COL_EVNO]
                if evno in self:
                    ref = self[evno]
                else:
                    LOG.error(u'Event %r in view not found in model', evno)
        return ref

    def getfirst(self):
        """Return the first event in the db."""
        ret = None
        if len(self.__index) > 0:
            ret = self[self.__index[0]]
        return ret

    def getnextrow(self, ref, scroll=True):
        """Return reference to the row one after current selection."""
        ret = None
        if ref is not None:
            path = self.__index.index(ref[u'evid']) + 1
            if path >= 0 and path < len(self.__index):
                ret = self[self.__index[path]]  # check reference
                if self.view:
                    self.__view_gotorow(path)
        return ret

    def getprevrow(self, ref, scroll=True):
        """Return reference to the row one after current selection."""
        ret = None
        if ref is not None:
            path = self.__index.index(ref[u'evid']) - 1
            if path >= 0 and path < len(self.__index):
                ret = self[self.__index[path]]  # check reference
                if self.view:
                    self.__view_gotorow(path)
        return ret

    def __view_gotorow(self, path):
        """Move view selection to the specified view path."""
        self.view.scroll_to_cell(path)
        self.view.set_cursor_on_cell(path)

    def __len__(self):
        return len(self.__store)

    def __getitem__(self, key):
        return self.__store[key]

    def __setitem__(self, key, value):
        self.__store[key] = value  # no change to key
        self.__notify(key)

    def __delitem__(self, key):
        self.__index.remove(key)
        del (self.__store[key])

    def __iter__(self):
        for r in self.__index:
            yield self.__store[r]

    def iterkeys(self):
        return self.__index.__iter__()

    def __contains__(self, item):
        return item in self.__store

    def __def_notify(self, data=None):
        """Notify view/model of changes in db."""
        if self.__viewmodel is not None:
            if data is None:  # re-build whole model
                self.__repopulate_viewmodel()
            else:
                try:
                    path = self.__index.index(data)
                    self.__viewmodel[path][COL_EVNO] = data
                    self.__viewmodel[path][COL_INFO] = self[data].event_info()
                    self.__viewmodel[path][COL_TYPE] = self[data].event_type()
                except Exception as e:
                    LOG.error(u'Index error %r: %s', data, e)

    def __repopulate_viewmodel(self):
        """Re-populate the view model."""
        self.__viewmodel.clear()
        for e in self.__index:
            nr = [e, self[e].event_info(), self[e].event_type()]
            self.__viewmodel.append(nr)

    def __init__(self, racetypes=None):
        """Constructor for the event db."""
        self.__index = []
        self.__store = {}
        self.__notify = self.__def_notify
        self.__viewmodel = None
        self.__viewparent = None
        self.__popup_startlist_cb = None
        self.__popup_startlist_data = None
        self.__popup_result_cb = None
        self.__popup_result_data = None
        self.__popup_program_cb = None
        self.__popup_program_data = None
        self.__evno_change_cb = None

        self.__lock = threading.Lock()
        self.view = None
        self.include_cols = DEFAULT_COLUMN_ORDER
        if racetypes is not None:
            self.racetypes = racetypes
        else:
            self.racetypes = defracetypes
