"""A collection of tools for preparing cycle race results."""

from __future__ import (division, absolute_import)

import os
import gtk
import glib
import gobject
import logging
import fcntl
import errno
from tempfile import NamedTemporaryFile
from shutil import copyfile
from metarace import jsonconfig

VERSION = u'1.12.2'
LIB_PATH = os.path.realpath(os.path.dirname(__file__))
UI_PATH = os.path.join(LIB_PATH, u'ui')
DB_PATH = os.path.join(LIB_PATH, u'data')
DATA_PATH = os.path.realpath(
    os.path.expanduser(os.path.join(u'~', u'Documents', u'metarace')))
DEFAULTS_PATH = os.path.join(DATA_PATH, u'.default')
LOGO_FILE = u'metarace_icon.svg'
SYSCONF_FILE = u'metarace.json'
PDF_TEMPLATE_FILE = u'pdf_template.json'
HTML_TEMPLATE_FILE = u'html_template.json'
LOGFILEFORMAT = u'%(asctime)s %(levelname)s:%(name)s: %(message)s'
LOGFORMAT = u'%(levelname)s:%(name)s: %(message)s'
LOGLEVEL = logging.DEBUG  # default console log level
sysconf = jsonconfig.config()  # system-defaults, populated by init() method
LOG = logging.getLogger(u'metarace')
LOG.setLevel(logging.DEBUG)


def init(withgtk=False):
    """Shared metarace program initialisation."""
    LOG.debug(u'library init withgtk=%r', withgtk)
    copyconf = mk_data_path()

    # Set global logging options
    logging._srcfile = None
    logging.logThreads = 0
    logging.logProcesses = 0

    # read in system configuration
    conffile = default_file(SYSCONF_FILE)
    try:
        if os.path.exists(conffile):
            LOG.debug(u'Loading system defaults from %r', conffile)
            with open(conffile, 'rb') as f:
                sysconf.read(f)
        else:
            LOG.info(u'System default file not found %r', conffile)
    except Exception as e:
        LOG.error(u'Error reading system config from %r: %s', conffile, e)

    # Do GTK init if required
    if withgtk:
        # Initialise threading in glib
        try:
            glib.threads_init()
        except Exception:
            LOG.debug(u'glib thread init failed, using gobject: %s', e)
            gobject.threads_init()

        # Initialise threading in GDK
        gtk.gdk.threads_init()

        try:
            # fix the default gnome menubar accelerator mapping
            mset = gtk.settings_get_default()
            mset.set_string_property('gtk-menu-bar-accel', 'F24', 'override')
            gtk.window_set_default_icon_from_file(default_file(LOGO_FILE))
        except Exception as e:
            LOG.debug(u'GTK init error: %s', e)
    # if required, create a new system default file
    if copyconf:
        LOG.info(u'Creating default system config %s', SYSCONF_FILE)
        with savefile(os.path.join(DEFAULTS_PATH, SYSCONF_FILE)) as f:
            sysconf.write(f)


def mainloop():
    """Call into the gtk main loop."""
    gtk.gdk.threads_enter()
    gtk.main()
    gtk.gdk.threads_leave()


def mk_data_path():
    """Create a shared data path if it does not yet exist."""
    ret = False
    if not os.path.exists(DATA_PATH):
        LOG.info(u'Creating data directory: %r', DATA_PATH)
        os.makedirs(DATA_PATH)
    if not os.path.exists(DEFAULTS_PATH):
        LOG.info(u'Creating system defaults directory: %r', DEFAULTS_PATH)
        os.makedirs(DEFAULTS_PATH)
        ret = True  # flag copy of config back to defaults path
    return ret


def config_path(configpath=None):
    """Clean and check argument for a writeable meet configuration path."""
    ret = None
    if configpath is not None:
        # sanitise into expected config path
        ret = configpath
        if not os.path.isdir(ret):
            ret = os.path.dirname(ret)  # assume dangling path contains file
        ret = os.path.realpath(ret)
        LOG.debug(u'Checking for meet %r using %r', configpath, ret)
        # then check if the path exists
        if not os.path.exists(ret):
            try:
                LOG.info(u'Creating meet folder %r', ret)
                os.makedirs(ret)
            except Exception as e:
                LOG.error(u'Unable to create folder %r: %s', ret, e)
                ret = None
        # check the path is writable
        if ret is not None:
            try:
                LOG.debug(u'Checking folder %r for write access', ret)
                with NamedTemporaryFile(dir=ret, prefix=u'.chkwrite_') as f:
                    pass
            except Exception as e:
                LOG.error(u'Unable to access meet folder %r: %s', ret, e)
                ret = None
    return ret


def default_file(filename=u''):
    """Return a path to the named file.

    Path components are stripped, then the the following locations
    are checked in order to find the first instance of filename:
        - current working directory
        - DEFAULTS_PATH
        - DB_PATH
    """
    filename = os.path.basename(filename)
    if filename in [u'..', u'.', u'', None]:
        return None

    ret = filename
    if os.path.exists(filename):
        pass
    else:
        check = os.path.join(DEFAULTS_PATH, filename)
        if os.path.exists(check):
            ret = check
        else:
            ## TODO: replace with lookup in package resources
            check = os.path.join(DB_PATH, filename)
            if os.path.exists(check):
                ret = check
    return ret


class savefile(object):
    """Tempfile-backed save file contextmanager."""

    def __init__(self, filename, tempdir=u'.'):
        self.__sfile = filename
        self.__path = tempdir
        self.__tfile = NamedTemporaryFile(mode='wb',
                                          suffix=u'.tmp',
                                          prefix=u'sav_',
                                          dir=self.__path,
                                          delete=False)

    def __enter__(self):
        return self.__tfile

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.__tfile.close()
        if exc_type is not None:
            return False  # raise exception
        # otherwise, file is saved ok in temp file
        os.chmod(self.__tfile.name, 0o644)
        try:
            os.rename(self.__tfile.name, self.__sfile)
            LOG.debug(u'os.rename: %r,%r', self.__tfile.name, self.__sfile)
        except OSError as e:
            LOG.debug(u'os.rename failed: %s', e)
            copyfile(self.__tfile.name, self.__sfile)
            LOG.warn(u'Un-safely moved file: %r', self.__sfile)
            os.unlink(self.__tfile.name)
        return True


def lockpath(configpath):
    """Open an advisory lock file in the meet config path."""
    lf = None
    lfn = os.path.join(configpath, u'.lock')
    try:
        lf = open(lfn, 'a+b')
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        LOG.debug(u'Config lock %r acquired', lfn)
    except Exception as e:
        if lf is not None:
            lf.close()
            lf = None
        LOG.error(u'Unable to acquire config lock %r: %s', lfn, e)
    return lf


def unlockpath(configpath, lockfile):
    """Release advisory lock and remove lock file."""
    lfn = os.path.join(configpath, u'.lock')
    os.unlink(lfn)
    lockfile.close()
    LOG.debug(u'Config lock %r released', lfn)
    return None


def about_dlg(window):
    """Display shared about dialog."""
    dlg = gtk.AboutDialog()
    dlg.set_transient_for(window)
    dlg.set_name(u'metarace')
    dlg.set_version(VERSION)
    dlg.set_copyright(
        u'Copyright \u00a9 2012-2022 Nathan Fraser and contributors')
    dlg.set_comments(u'Cycle Race Toolkit')
    dlg.set_license(LICENSETEXT)
    dlg.run()
    dlg.destroy()


LICENSETEXT = """
MIT License

Copyright (c) 2012-2022 Nathan Fraser and contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
