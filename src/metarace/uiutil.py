
"""Shared gtk UI helper functions."""

import os
import gtk
import gobject
from pango import (FontDescription, STYLE_OBLIQUE, STYLE_NORMAL)
import metarace
from metarace import tod
from metarace import strops

# Font-overrides
DIGITFONT = FontDescription(u'Noto Mono Medium 22')
MONOFONT = FontDescription(u'Noto Mono')
LOGVIEWFONT = FontDescription(u'Noto Mono 11')

# Button indications
bg_none = gtk.image_new_from_file(metarace.default_file(u'bg_idle.svg'))
bg_armstart = gtk.image_new_from_file(metarace.default_file(u'bg_armstart.svg'))
bg_armint = gtk.image_new_from_file(metarace.default_file(u'bg_armint.svg'))
bg_armfin = gtk.image_new_from_file(metarace.default_file(u'bg_armfin.svg'))

def hvscroller(child):
    """Return a new scrolled window packed with the supplied child."""
    vs = gtk.ScrolledWindow()
    vs.show()
    vs.set_border_width(5)
    vs.set_shadow_type(gtk.SHADOW_IN)
    vs.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
    vs.add(child)
    return vs

def vscroller(child):
    """Return a new scrolled window packed with the supplied child."""
    vs = gtk.ScrolledWindow()
    vs.show()
    vs.set_border_width(5)
    vs.set_shadow_type(gtk.SHADOW_IN)
    vs.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
    vs.add(child)
    return vs

class statbut(object):
    def __init__(self, b):
        c = gtk.HBox(spacing=2)
        self._i = gtk.image_new_from_pixbuf(bg_none.get_pixbuf())
        #self._i = gtk.image_new_from_file(metarace.default_file(u'bg_idle.svg'))
        self._i.show()
        c.pack_start(self._i, False, True, 0)
        self._l = gtk.Label(u'Idle')
        self._l.show()
        c.pack_start(self._l, True, True, 0)
        c.show()
        b.add(c)
        self._b = b

    def buttonchg(self, image, label=None):
        self._i.set_from_pixbuf(image.get_pixbuf())
        if label is not None:
            self._l.set_text(label)

    def set_sensitive(self, sensitive=False):
        self._b.set_sensitive(sensitive)

def mkviewcoltod(view=None, header=u'', cb=None, width=120,
                 editcb=None, colno=None):
    """Return a Time of Day view column."""
    i = gtk.CellRendererText()
    i.set_property(u'xalign', 1.0)
    j = gtk.TreeViewColumn(header, i)
    j.set_cell_data_func(i, cb, colno)
    if editcb is not None:
        i.set_property(u'editable', True)
        i.connect(u'edited', editcb, colno)
    j.set_min_width(width)
    view.append_column(j)
    return j

def mkviewcoltxt(view=None, header=u'', colno=None, cb=None,
                  width=None, halign=None, calign=None,
                  expand=False, editcb=None, maxwidth=None,
                  bgcol=None, fontdesc=None, fixed=False):
    """Return a text view column."""
    i = gtk.CellRendererText()
    if cb is not None:
        i.set_property(u'editable', True)
        i.connect(u'edited', cb, colno)
    if calign is not None:
        i.set_property(u'xalign', calign)
    if fontdesc is not None:
        i.set_property(u'font_desc', fontdesc)
    j = gtk.TreeViewColumn(header, i, text=colno)
    if bgcol is not None:
        j.add_attribute(i, u'background', bgcol)
    if halign is not None:
        j.set_alignment(halign)
    if fixed:
        j.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
    if expand:
        if width is not None:
            j.set_min_width(width)
        j.set_expand(True)
    else:
        if width is not None:
            j.set_min_width(width)
    if maxwidth is not None:
        j.set_max_width(maxwidth)
    view.append_column(j)
    if editcb is not None:
        i.connect(u'editing-started', editcb)
    return i

def mkviewcolbg(view=None, header=u'', colno=None, cb=None,
                  width=None, halign=None, calign=None,
                  expand=False, editcb=None, maxwidth=None):
    """Return a text view column."""
    i = gtk.CellRendererText()
    if cb is not None:
        i.set_property(u'editable', True)
        i.connect(u'edited', cb, colno)
    if calign is not None:
        i.set_property(u'xalign', calign)
    j = gtk.TreeViewColumn(header, i, background=colno)
    if halign is not None:
        j.set_alignment(halign)
    if expand:
        if width is not None:
            j.set_min_width(width)
        j.set_expand(True)
    else:
        if width is not None:
            j.set_min_width(width)
    if maxwidth is not None:
        j.set_max_width(maxwidth)
    view.append_column(j)
    if editcb is not None:
        i.connect(u'editing-started', editcb)
    return i

def savecsvdlg(title=u'', parent=None, hintfile=None, lpath=None):
    ret = None
    dlg = gtk.FileChooserDialog(title, parent,
        gtk.FILE_CHOOSER_ACTION_SAVE, (gtk.STOCK_CANCEL,
        gtk.RESPONSE_CANCEL, gtk.STOCK_SAVE, gtk.RESPONSE_OK))
    cfilt = gtk.FileFilter()
    cfilt.set_name(u'CSV Files')
    cfilt.add_mime_type(u'text/csv')
    cfilt.add_pattern(u'*.csv')
    dlg.add_filter(cfilt)
    cfilt = gtk.FileFilter()
    cfilt.set_name(u'All Files')
    cfilt.add_pattern(u'*')
    dlg.add_filter(cfilt)
    if lpath:
        dlg.set_current_folder(lpath)
    if hintfile:
        dlg.set_current_name(hintfile)
    response = dlg.run()
    if response == gtk.RESPONSE_OK:
        ret = dlg.get_filename().decode(u'utf-8')
    dlg.destroy()
    return ret

def loadcsvdlg(title=u'', parent=None, lpath=None):
    ret = None
    dlg = gtk.FileChooserDialog(title, parent,
            gtk.FILE_CHOOSER_ACTION_OPEN, (gtk.STOCK_CANCEL,
            gtk.RESPONSE_CANCEL, gtk.STOCK_OPEN, gtk.RESPONSE_OK))
    cfilt = gtk.FileFilter()
    cfilt.set_name(u'CSV Files')
    cfilt.add_mime_type(u'text/csv')
    cfilt.add_pattern(u'*.csv')
    dlg.add_filter(cfilt)
    cfilt = gtk.FileFilter()
    cfilt.set_name(u'All Files')
    cfilt.add_pattern(u'*')
    dlg.add_filter(cfilt)
    if lpath:
        dlg.set_current_folder(lpath)
    response = dlg.run()
    if response == gtk.RESPONSE_OK:
        ret = dlg.get_filename().decode(u'utf-8')
    dlg.destroy()
    return ret

def mkviewcolbool(view=None, header=u'', colno=None, cb=None,
                  width=None, expand=False):
    """Return a boolean view column."""
    i = gtk.CellRendererToggle()
    i.set_property(u'activatable', True)
    if cb is not None:
        i.connect(u'toggled', cb, colno)
    j = gtk.TreeViewColumn(header, i, active=colno)
    if expand:
        j.set_min_width(width)
        j.set_expand(True)
    else:
        if width is not None:
            j.set_min_width(width)
    view.append_column(j)
    return i

def coltxtbibser(col, cr, model, iter, data):
    """Display a bib.ser string in a tree view."""
    (bibcol, sercol) = data
    cr.set_property(u'text', 
                    strops.bibser2bibstr(model.get_value(iter, bibcol),
                                         model.get_value(iter, sercol)))

def mkviewcolbibser(view=None, header=u'No.', bibno=0, serno=1,
                    width=None, expand=False):
    """Return a column to display bib/series as a bib.ser string."""
    i = gtk.CellRendererText()
    i.set_property(u'xalign', 1.0)
    j = gtk.TreeViewColumn(header, i)
    j.set_cell_data_func(i, coltxtbibser, (bibno, serno))
    if expand:
        j.set_min_width(width)
        j.set_expand(True)
    else:
        if width is not None:
            j.set_min_width(width)
    view.append_column(j)
    return i

def mktextentry(prompt, row, table):
    """Create and return a text entry within a gtk table."""
    if u'?' not in prompt:
        prompt += u':'
    l = gtk.Label(prompt)
    l.set_alignment(0.0, 0.5)
    l.show()
    table.attach(l, 0, 1, row, row+1, gtk.FILL, gtk.FILL, xpadding=5)
    e = gtk.Entry()
    e.set_width_chars(24)
    e.set_activates_default(True)	# Check assumption on window
    e.show()
    table.attach(e, 1, 2, row, row+1, gtk.FILL|gtk.EXPAND, gtk.FILL,
                   xpadding=5, ypadding=2)
    return e

def mkcomboentry(prompt, row, table, options):
    """Create and return a combo entry within a gtk table."""
    l = gtk.Label(prompt)
    l.set_alignment(1.0, 0.5)
    l.show()
    table.attach(l, 0, 1, row, row+1, gtk.FILL, gtk.FILL, xpadding=5)
    c = gtk.combo_box_new_text()
    c.show()
    for opt in options:
        c.append_text(opt)
    table.attach(c, 1, 2, row, row+1, gtk.FILL, gtk.FILL, xpadding=5)
    return c

def mklbl(prompt, row, table):
    """Create and return label within a gtk table."""
    l = gtk.Label(prompt)
    l.set_alignment(1.0, 0.5)
    l.show()
    table.attach(l, 0, 1, row, row+1, gtk.FILL, gtk.FILL, xpadding=5)
    e = gtk.Label()
    e.set_alignment(0.0, 0.5)
    e.show()
    table.attach(e, 1, 2, row, row+1, gtk.FILL, gtk.FILL, xpadding=5)
    return e

def mkbutintbl(prompt, row, col, table):
    """Create and return button within a gtk table."""
    b = gtk.Button(prompt)
    b.show()
    table.attach(b, col, col+1, row, row+1, gtk.FILL, gtk.FILL,
                 xpadding=5, ypadding=5)
    return b

def questiondlg(window, question, subtext=None):
    """Display a question dialog and return True/False."""
    dlg = gtk.MessageDialog(window, gtk.DIALOG_MODAL,
                            gtk.MESSAGE_QUESTION, gtk.BUTTONS_YES_NO,
                            question)
    if subtext is not None:
        dlg.format_secondary_text(subtext)
    ret = False
    response = dlg.run()
    if response == gtk.RESPONSE_YES:
        ret = True
    dlg.destroy()
    return ret    

def now_button_clicked_cb(button, entry=None):
    """Copy the current time of day into the supplied entry."""
    if entry is not None:
        entry.set_text(tod.now().timestr())

def edit_times_dlg(window, stxt=None, ftxt=None,
                           btxt=None, ptxt=None,
                           bonus=False, penalty=False):
    """Display times edit dialog and return updated time strings."""
    b = gtk.Builder()
    b.add_from_file(os.path.join(metarace.UI_PATH, u'edit_times.ui'))
    dlg = b.get_object(u'timing')
    dlg.set_transient_for(window)

    se = b.get_object(u'timing_start_entry')
    se.modify_font(MONOFONT)
    if stxt is not None:
        se.set_text(stxt)
    b.get_object(u'timing_start_now').connect(u'clicked',
                  now_button_clicked_cb, se)

    fe = b.get_object(u'timing_finish_entry')
    fe.modify_font(MONOFONT)
    if ftxt is not None:
        fe.set_text(ftxt)
    b.get_object(u'timing_finish_now').connect(u'clicked',
                  now_button_clicked_cb, fe)

    be = b.get_object(u'timing_bonus_entry')
    be.modify_font(MONOFONT)
    if btxt is not None:
        be.set_text(btxt)
    if bonus:
        be.show()
        b.get_object(u'timing_bonus_label').show()

    pe = b.get_object(u'timing_penalty_entry')
    pe.modify_font(MONOFONT)
    if ptxt is not None:
        pe.set_text(ptxt)
    if penalty:
        pe.show()
        b.get_object(u'timing_penalty_label').show()

    ret = dlg.run()
    stxt = se.get_text().decode(u'utf-8').strip()
    ftxt = fe.get_text().decode(u'utf-8').strip()
    btxt = be.get_text().decode(u'utf-8').strip()
    ptxt = pe.get_text().decode(u'utf-8').strip()
    dlg.destroy()
    return (ret, stxt, ftxt, btxt, ptxt)
