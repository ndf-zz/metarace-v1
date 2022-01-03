
"""Custom log handlers."""

import logging
import glib

class traceFilter(logging.Filter):
    """Filter events to type TIMER only."""
    def filter(self, record):
        return record.levelno == 25	# TIMER LOG LEVEL

class traceHandler(logging.Handler):
    """Class for capturing timer log traces."""
    def __init__(self, trace=None):
        self.__trace = trace
        logging.Handler.__init__(self)
        self.addFilter(traceFilter())

    def emit(self, record):
        """Append log record to trace."""
        if self.__trace is not None:
            msg = self.format(record)
            self.__trace.append(msg)

class textViewHandler(logging.Handler):
    """A class for displaying log messages in a GTK text view."""

    def __init__(self, log=None, view=None, scroll=None):
        self.log = log
        self.view = view
        self.scroll = scroll
        self.scroll_pending = False
        logging.Handler.__init__(self)

    def do_scroll(self):
        """Catchup end of scrolled window."""
        if self.scroll_pending:
            self.view.scroll_to_iter(self.log.get_end_iter(), 0)
            self.scroll_pending = False
        return False
    
    def append_log(self, msg):
        """Append msg to the text view."""
        atend = True
        if self.scroll and self.scroll.page_size > 0:
            # Fudge a 'sticky' end of scroll mode... about a pagesz
            if self.scroll.upper - (self.scroll.value
                   + self.scroll.page_size) > (0.5 * self.scroll.page_size):
                atend = False
        self.log.insert(self.log.get_end_iter(), msg.strip() + '\n')
        if atend:
            self.scroll_pending = True
            glib.timeout_add_seconds(1, self.do_scroll)
        return False

    def emit(self, record):
        """Emit log record to gtk main loop."""
        msg = self.format(record)
        glib.idle_add(self.append_log, msg)

class statusHandler(logging.Handler):
    """A class for displaying log messages in a GTK status bar."""

    def __init__(self, status=None, context=0):
        self.status = status
        self.context = context
        logging.Handler.__init__(self)

    def pull_status(self, msgid):
        """Remove specified msgid from the status stack."""
        self.status.remove_message(self.context, msgid)
        return False

    def push_status(self, msg, level):
        """Push the given msg onto the status stack, and defer removal."""
        delay = 3
        if level > 25:
            delay = 8
        msgid = self.status.push(self.context, msg)
        glib.timeout_add_seconds(delay, self.pull_status, msgid)
        return False

    def emit(self, record):
        """Emit log record to gtk main loop."""
        msg = self.format(record)
        glib.idle_add(self.push_status, msg, record.levelno)

