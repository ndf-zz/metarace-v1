"""export result files in a thread"""

import threading
import subprocess
import logging
import os

import metarace

MIRROR_CMD = u'echo'  # Command/Argument defaults
MIRROR_ARGS = [u'dummy', u'srcdir={srcdir}', u'dstdir={dstdir}']
_log = logging.getLogger(u'metarace.export')
_log.setLevel(logging.DEBUG)


class mirror(threading.Thread):
    """Mirror thread object class."""

    def __init__(self,
                 callback=None,
                 callbackdata=None,
                 localpath=u'.',
                 remotepath=None,
                 mirrorcmd=None,
                 arguments=None,
                 data=None):
        """Construct mirror thread object."""
        threading.Thread.__init__(self)
        self.daemon = True
        self.__cb = None
        if callback is not None:
            self.__cb = callback
        self.__cbdata = None
        if callbackdata is not None:
            self.__cbdata = callbackdata
        self.__localpath = localpath
        self.__remotepath = u''
        if remotepath is not None:
            self.__remotepath = remotepath

        # config starts with module defaults
        self.__mirrorcmd = MIRROR_CMD
        self.__arguments = MIRROR_ARGS

        # then overwrite from sysconf - if present
        if metarace.sysconf.has_section(u'export'):
            if metarace.sysconf.has_option(u'export', u'command'):
                self.__mirrorcmd = metarace.sysconf.get(u'export', u'command')
            if metarace.sysconf.has_option(u'export', u'arguments'):
                self.__arguments = metarace.sysconf.get(
                    u'export', u'arguments')

        # and then finally allow override in object creation
        if mirrorcmd:
            self.__mirrorcmd = mirrorcmd
        if arguments:
            self.__arguments = arguments

        self.__data = data

    def set_remotepath(self, pathstr):
        """Set or clear the remote path value."""
        self.__remotepath = pathstr

    def set_localpath(self, pathstr):
        """Set or clear the local path value."""
        self.__localpath = pathstr

    def set_cb(self, func=None, cbdata=None):
        """Set or clear the event callback."""
        # if func is not callable, gtk mainloop will catch the error
        if func is not None:
            self.__cb = func
            self.__cbdata = cbdata
        else:
            self.__cb = None
            self.__cbdata = None

    def run(self):
        """Called via threading.Thread.start()."""
        running = True
        _log.debug(u'Starting')
        ret = None
        try:
            # format errors in arguments caught as exception
            arglist = [
                a.format(srcdir=self.__localpath,
                         dstdir=self.__remotepath,
                         command=self.__mirrorcmd,
                         data=self.__data) for a in self.__arguments
            ]
            arglist.insert(0, self.__mirrorcmd)

            _log.debug(u'Calling subprocess: %r', arglist)
            # TODO: convert to run w/timeout and I/O capture (py3)
            ret = subprocess.check_call(arglist)
            if self.__cb:
                self.__cb(ret, self.__cbdata)
        except Exception as e:
            _log.error(u'%s: %s', e.__class__.__name__, e)
        _log.info(u'Complete: Returned %r', ret)
