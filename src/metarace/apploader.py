
"""Event loader application."""

import pygtk
pygtk.require("2.0")

import gtk
import glib
import gobject

import os
import sys
import logging
import datetime
import metarace

from metarace import jsonconfig
from metarace import strops
from metarace import uiutil
from metarace import eventdb
#from metarace import trackmeet
#from metarace import roadmeet

LOG = logging.getLogger(u'metarace.apploader')
LOG.setLevel(logging.DEBUG)

def meet_identify(configpath=None):
    """Try to identify the meet located at configpath, returning info."""
    # Returns an array:
    # [type, configpath, descr, evtinfo, datestr]
    ret = None
    cfile = os.path.join(configpath, u'config.json')
    efile = os.path.join(configpath, u'events.csv')
    if os.path.exists(cfile):
        cr = jsonconfig.config()
        try:
            LOG.debug(u'Reading meet config %r', cfile)
            with open(cfile, 'rb') as f:
                cr.read(f)
            edb = eventdb.eventdb()
            LOG.debug(u'Reading meet events %r', efile)
            edb.load(efile)
            if cr.has_section(u'trackmeet'):	# this is TRACK
                descr = u''
                if cr.has_option(u'trackmeet', u'title'):
                    descr = cr.get(u'trackmeet', u'title') + u' '
                if cr.has_option(u'trackmeet', u'subtitle'):
                    descr += cr.get(u'trackmeet', u'subtitle') + u' '
                ecstr = u'New Meet'
                ecount = len(edb)
                if ecount:
                    ecstr = unicode(ecount) + u' events'
                infostr = u''
                if cr.has_option(u'trackmeet', u'date'):
                    infostr = cr.get(u'trackmeet', u'date')
                LOG.debug(u'Track meet: %s/%s/%s', descr, ecstr, infostr)
                ret = [u'track', configpath,
                        descr.strip(), ecstr, infostr]
            elif cr.has_section(u'roadmeet'):	# this is Road
                etype = u'rms'
                eh = edb.getfirst()
                if eh is not None and eh[u'type']:
                    etype = eh[u'type']
                descr = u''
                if cr.has_option(u'roadmeet', u'title'):
                    descr = cr.get(u'roadmeet', u'title') + u' '
                if cr.has_option(u'roadmeet', u'subtitle'):
                    descr += cr.get(u'roadmeet', u'subtitle') + u' '
                infostr = u''
                if cr.has_option(u'roadmeet', u'date'):
                    infostr = cr.get(u'roadmeet', u'date')
                LOG.debug(u'Road meet: %s/%s/%s', descr, etype, infostr)
                ret = [u'road', configpath,
                       descr.strip(), etype, infostr]
            else:
                LOG.warning(u'Meet type could not be identified')
        except Exception as e:
            LOG.error(u'Error reading meet folder %r: %s', configpath, e)
    return ret
    
class apploader(object):
    """Event loader."""
    def destroy_cb(self, window, msg=u''):
        """Handle destroy signal and exit application."""
        self.window.hide()
        glib.idle_add(self.destroy_handler)

    def create_new_event(self, path=None):
        """Create new event of the specified type at path."""
        if path is None or not path:
            path = unicode(datetime.datetime.now().strftime('%F_%H%M%S'))

        # load up dialog and fill path
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'create_event.ui'))
        dlg = b.get_object('create')
        dlg.set_transient_for(self.window)
        etsel = b.get_object('create_type')
        etsel.set_active(1)
        etmodel = b.get_object('type_model')
        pent = b.get_object('path_entry')
        pent.set_text(path)
        
        response = dlg.run()
        dlg.hide()
        if response == 1:
            np = pent.get_text().decode(u'utf8')
            nt = etmodel[etsel.get_active()][0]
            print(u'CREATE: ' + repr(nt) + u' / ' + repr(np))

            # if ok, create path - if necessary
            if not os.path.exists(np):
                print(u'Creating new event path: ' + repr(np))
                os.makedirs(np)
            oktogo = True
            # refuse to continue if path contains a config.json
            if os.path.exists(os.path.join(np, u'config.json')):
                over = uiutil.questiondlg(self.window,
                         u'Overwrite existing event?',
                         u'The chosen path appears to already contain an event. Overwriting the event will destroy any existing event information.')
                if over:
                    os.unlink(os.path.join(np, u'config.json'))
                    os.unlink(os.path.join(np, u'events.csv'))
                else:
                    oktogo = False
            # validate type
            tv = nt.split(u':')
            etype = None
            subtype = None
            if len(tv) < 1 or len(tv) > 2:
                print(u'Invalid type')
                oktogo = False
            else:
                etype = tv[0]
                if len(tv) > 1:
                    subtype = tv[1]

            if oktogo:
                print(u'CREATE: ' + repr(etype) + u':' + repr(subtype) + u' at: ' + repr(np))
                cw = jsonconfig.config()
                ## Question: merge in defaults here? For now, no
                if etype == u'track':
                    cw.add_section(u'trackmeet')
                else:
                    cw.add_section(u'roadmeet')
                    if subtype:
                        edb = eventdb.eventdb([])
                        event = edb.add_empty(evno=u'00')
                        event[u'type'] = subtype
                        edb.save(os.path.join(np, u'events.csv'))
                with open(os.path.join(np, u'config.json'), 'wb') as f:
                    cw.write(f)
            glib.idle_add(self.loadpath)
        else:
            print(u'CREATE: cancelled.')

        dlg.destroy()
        return False	# if called from idle_add

    def create_but_clicked_cb(self, button, data=None):
        """Handle create new event."""
        self.create_new_event()

    def show(self):
        self.window.show()

    def shutdown(self, data=None):
        pass

    def destroy_handler(self):
        gtk.main_quit()
        return False

    def infostring(self, maintype, subtype):
        """Return a human-readable type string."""
        ret = u'Unknown'
        if maintype == u'road':
            if subtype == u'rhcp':
                ret = u'Road Handicap'
            elif subtype == u'rms':
                ret = u'Road Race'
            elif subtype == u'irtt':
                ret = u'Road Time Trial'
            elif subtype == u'trtt':
                ret = u'Team Time Trial'
            elif subtype == u'crit':
                ret = u'Criterium/Kermesse'
            elif subtype == u'sportif':
                ret = u'Sportif'
            elif subtype == u'cross':
                ret = u'Cyclocross'
            elif subtype == u'hour24':
                ret = u'24 Hour Road Race'
        elif maintype == u'track':
            ret = u'Track (' + subtype.title() + ')'
        return ret

    def run_event(self, etype, epath, data=None):
        """Load and run the selected event."""
        cfpath = os.path.abspath(epath)
        print(u'Running ' + repr(etype) + u'at: ' + repr(cfpath))
        if etype == u'track':
            os.chdir(cfpath)
            self.window.hide()
            #from metarace import trackmeet
            self.subapp = trackmeet.trackmeet(cfpath)
            self.subapp.loadconfig()
            self.subapp.window.show()
            self.subapp.start()
        else:
            os.chdir(cfpath)
            self.window.hide()
            #from metarace import roadmeet
            self.subapp = roadmeet.roadmeet(cfpath, u'rms')
            self.subapp.loadconfig()
            self.subapp.window.show()
            self.subapp.start()

    def row_activated_cb(self, view, path, column, data=None):
        """Handle activate on a view row."""
        row = self.model[path]
        self.run_event(row[6], row[0])

    def open_but_clicked_cb(self, button, data=None):
        """Handle click on open button."""
        ## IF Row Selected, try to open that one, else open file chooser..
        sel = self.view.get_selection()
        cnt = sel.count_selected_rows()
        if cnt > 0:	# Degenerate path
            (model, iters) = sel.get_selected_rows()
            elist = [(model[i][6],model[i][0]) for i in iters]
            if len(elist) == 1:
                print(u'Open event: ' + repr(elist))
                return self.run_event(elist[0][0], elist[0][1])
            else:
                print(u'Unable to open event: ' + repr(elist))
                return False
        else:
            print(u'No selection, continue to open dialog...')
        path = None
        dlg = gtk.FileChooserDialog(u'Open metarace event', self.window,
            gtk.FILE_CHOOSER_ACTION_SELECT_FOLDER, (gtk.STOCK_CANCEL,
            gtk.RESPONSE_CANCEL, gtk.STOCK_OPEN, gtk.RESPONSE_OK))
        dlg.set_current_folder(self.path)
        response = dlg.run()
        if response == gtk.RESPONSE_OK:
            path = dlg.get_filename()
        else:
            pass
        dlg.destroy()

        if path is not None:
            event = meet_identify(path)
            if event is not None:
                self.run_event(event[0], event[1])
            else:
                self.create_new_event(path)
                # one level of repeat for the special case
                event = meet_identify(path)
                if event is not None:
                    self.run_event(event[0], event[1])

    def loadpath(self, data=None):
        """Load the event list."""
        self.model.clear()
        for root, dirs, files in os.walk(self.path):
            #print(u'dirs iter: ' + repr([root, dirs, files]))
            if root == self.path:
                for d in dirs:
                    if d != u'.default':	# ignore config dir
                        event = meet_identify(os.path.join(root, d))
                        if event is not None:
                            tinfo = self.infostring(event[0], event[3])
                            self.model.append([event[1], d, event[2],
                                               tinfo, event[4], u'',
                                               event[0],u''])
            del dirs[0:]
        return False

    def __init__(self, path=None):
        """App constructor."""
        self.path = u'.'
        if path is not None:
            self.path = path
        self.subapp = None
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'mr_init.ui'))
        self.window = b.get_object('window')


        self.model = gtk.ListStore(gobject.TYPE_STRING, # fullpath
                                   gobject.TYPE_STRING, # shortpath
                                   gobject.TYPE_STRING, # descr
                                   gobject.TYPE_STRING, # type
                                   gobject.TYPE_STRING, # date
                                   gobject.TYPE_STRING, # extra info
                                   gobject.TYPE_STRING, # class road/track
                                   gobject.TYPE_STRING) # sorting?
        t = gtk.TreeView(self.model)
        #t.set_reorderable(True)
        t.set_rules_hint(True)
        #t.set_headers_clickable(True)
        self.model.set_sort_column_id(1,gtk.SORT_ASCENDING)
        #t.set_headers_visible(False)
        t.set_search_column(1)
        uiutil.mkviewcoltxt(t, 'Path', 1,width=100)
        uiutil.mkviewcoltxt(t, 'Description', 2,expand=True,maxwidth=300)
        uiutil.mkviewcoltxt(t, 'Type', 3,width=100)
        t.connect('row-activated', self.row_activated_cb)
        t.show()
        self.view = t
        b.get_object('scrollbox').add(t)
        b.connect_signals(self)
        glib.idle_add(self.loadpath)

def appmain():
    configpath = metarace.config_path(metarace.DATA_PATH)
    if configpath is None:
        LOG.error(u'Unable to open directory %r', sys.argv[1])
        sys.exit(1)
    os.chdir(configpath)
    metarace.init(withgtk=True)
    app = apploader()
    app.show()
    try:
        metarace.mainloop()
    except:
        app.shutdown(u'Exception from main loop.')
        if app.subapp is not None:
            app.subapp.shutdown()
        raise

def meetmain(cfpath):
    configpath = metarace.config_path(cfpath)
    if configpath is None:
        LOG.error(u'Unable to open meet config %r', sys.argv[1])
        sys.exit(1)
    lf = metarace.lockpath(configpath)
    if lf is None:
        LOG.error(u'Unable to lock meet config, already in use')
        sys.exit(1)
    LOG.debug(u'Entering meet folder %r', configpath)
    os.chdir(configpath)
    metarace.init(withgtk=True)
    app = None
    meet = meet_identify(configpath)
    if meet is not None:
        LOG.info(u'Loading %s', meet[2])
        if meet[0] == u'track':
            from metarace import trackmeet
            app = trackmeet.trackmeet()
        else:
            from metarace import roadmeet
            app = roadmeet.roadmeet(configpath, meet[3])
    else:
        LOG.error(u'Meet not found in %r', configpath)
        sys.exit(1)
    app.loadconfig()
    app.window.show()
    app.start()
    try:
        metarace.mainloop()
        lf = metarace.unlockpath(configpath, lf)
    except:
        app.shutdown(u'Exception from main loop')
        raise

def main():
    """Run the app loader."""
    # check for a provided path
    if len(sys.argv) > 2:
        print(u'Usage: metarace [configdir]\n')
        sys.exit(1)
    elif len(sys.argv) == 2:
        meetmain(sys.argv[1])
    else:
        appmain()

if __name__ == '__main__':
    # attach a console log handler to the root logger
    ch = logging.StreamHandler()
    ch.setLevel(metarace.LOGLEVEL)
    fh = logging.Formatter(metarace.LOGFORMAT)
    ch.setFormatter(fh)
    logging.getLogger().addHandler(ch)
    main()
