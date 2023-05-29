"""Time of Day types and functions

Time of Day (tod) records are used to compute net times,
and to communicate context of timing events. Each tod object
includes the following properties:

	timeval	decimal number of seconds, to 4 places
	index	optional serial number or marker (from timing device)
	chan	optional timing channel number or indicator
	refid	optional transponder id or rider identifier
	source	optional source id of timing device that generated the tod

Two specific types are provided:

	tod	Time of Day, strictly >= 0 and less than 24 hours,
		arithmetic is mod 24 hours.
	agg	Aggregate time, may be greater than 24 hours,
		negative values permitted.

Supported arithmetic operations:

		Y:	tod	agg	int	decimal
	tod - Y		yes	no	no	no	*
	tod + Y		yes	no	no	no	*
	agg - Y		yes	yes	yes	yes
	agg + Y		yes	yes	yes	yes

	* Result is mod 24 hours

"""
from __future__ import division

import decimal
import re
import logging
from datetime import datetime
from bisect import bisect_left as _bisect

# module log object
_log = logging.getLogger(u'metarace.tod')
_log.setLevel(logging.DEBUG)

# Formatting and truncation constants
_QUANT_5PLACES = decimal.Decimal(u'0.00001')
_QUANT_4PLACES = decimal.Decimal(u'0.0001')
_QUANT_3PLACES = decimal.Decimal(u'0.001')
_QUANT_2PLACES = decimal.Decimal(u'0.01')
_QUANT_1PLACE = decimal.Decimal(u'0.1')
_QUANT_0PLACES = decimal.Decimal(u'1')
_QUANT = [
    _QUANT_0PLACES, _QUANT_1PLACE, _QUANT_2PLACES, _QUANT_3PLACES,
    _QUANT_4PLACES, _QUANT_5PLACES
]
_QUANT_FW = [2, 4, 5, 6, 7, 8]
_QUANT_TWID = [8, 10, 11, 12, 13, 14]
_QUANT_PAD = [u'     ', u'   ', u'  ', u' ', u'', u'']
_QUANT_OPAD = [u'    ', u'  ', u' ', u'', u'', u'']
_MILL = decimal.Decimal(1000000)


def now(index=u'', chan=u'CLK', refid=u'', source=u'host'):
    """Return a tod set to the current local time."""
    return tod(_now2dec(), index, chan, refid, source)


def mkagg(timeval=u''):
    """Return agg for given timeval or None"""
    ret = None
    if timeval is not None and timeval != u'':
        try:
            ret = agg(timeval)
        except Exception as e:
            _log.debug(u'mkagg() %s: %s', e.__class__.__name__, e)
    return ret


def mktod(timeval=u''):
    """Return tod for given timeval or None"""
    ret = None
    if timeval is not None and timeval != u'':
        try:
            ret = tod(timeval)
        except Exception as e:
            _log.debug(u'mktod() %s: %s', e.__class__.__name__, e)
    return ret


def _now2dec():
    """Create a decimal timevalue for the current local time."""
    dv = datetime.now()
    ret = (dv.microsecond / _MILL).quantize(_QUANT_4PLACES)
    ret += 3600 * dv.hour + 60 * dv.minute + dv.second
    return ret


def _dec2hm(dectod=None):
    """Return truncated time string in hours and minutes"""
    strtod = None
    if dectod is not None:
        if dectod >= 3600:  # 'HH:MM'
            strtod = u'{0}:{1:02}'.format(
                int(dectod) // 3600, (int(dectod) % 3600) // 60)
        else:  # 'M'
            strtod = u'{0}'.format(int(dectod) // 60)
    return strtod


def _dec2str(dectod=None, places=4, zeros=False, hoursep=u'h', minsep=u':'):
    """Return formatted string for given tod decimal value

    Optional argument 'zeros' will use leading zero chars
    up to 24 hours. eg:

             '00h00:01.2345'   zeros=True
                    '1.2345'   zeros=False
    """
    strtod = None
    if dectod is not None:
        sign = u''
        # quantize first to preserve down rounding
        dv = dectod.quantize(_QUANT[places], rounding=decimal.ROUND_FLOOR)
        if dv < 0:
            dv = dv.copy_negate()
            sign = u'-'
        if zeros or dv >= 3600:  # '-HhMM:SS.dcmz'
            fmt = u'{0}{1}{2}{3:02}{4}{5:0{6}}'
            if zeros:  # '-00h00:0S.dcmz'
                fmt = u'{0}{1:02}{2}{3:02}{4}{5:0{6}}'
            strtod = fmt.format(sign,
                                int(dv) // 3600, hoursep,
                                (int(dv) % 3600) // 60, minsep, dv % 60,
                                _QUANT_FW[places])
        elif dv >= 60:  # '-M:SS.dcmz'
            strtod = u'{0}{1}{2}{3:0{4}}'.format(sign,
                                                 int(dv) // 60, minsep,
                                                 dv % 60, _QUANT_FW[places])
        else:  # '-S.dcmz'
            strtod = u'{0}{1}'.format(sign, dv)
    return strtod


def _str2dec(timestr=u''):
    """Return decimal for given string.

    Attempt to match against patterns:

    	-HhMM:SS.dcmz		Canonical
    	-H:MM:SS.dcmz		Omega
    	-H:MM'SS"dcmz		Chronelec
    	-H-MM-SS.dcmz		Keypad entry
    """
    dectod = None
    timestr = timestr.strip()  # assumes string
    if timestr == u'now':
        dectod = _now2dec()
    else:
        m = re.match(
            r'^(-?)(?:(?:(\d+)[h:-])?(\d{1,2})[:\'-])?(\d{1,2}(?:[\.\"]\d+)?)$',
            timestr,
            flags=re.UNICODE)
        if m is not None:
            dectod = decimal.Decimal(m.group(4).replace(u'"', u'.'))
            dectod += decimal.Decimal(m.group(3) or 0) * 60
            dectod += decimal.Decimal(m.group(2) or 0) * 3600
            if m.group(1):  # negative sign
                dectod = dectod.copy_negate()
        else:
            dectod = decimal.Decimal(timestr)
    return dectod


def _tv2dec(timeval):
    """Convert the provided value into a decimal timeval for tod/agg."""
    ret = 0
    if isinstance(timeval, decimal.Decimal):
        ret = timeval
    elif isinstance(timeval, basestring):
        ret = _str2dec(timeval)
    elif isinstance(timeval, tod):
        # Discard context on supplied tod and copy decimal obj
        ret = timeval.timeval
    elif isinstance(timeval, float):
        # Round off float to max tod precision
        ret = decimal.Decimal(u'{0:0.4f}'.format(timeval))
    else:
        ret = decimal.Decimal(timeval)
    return ret


class tod(object):
    """A class for working with short time intervals using time of day"""

    def __init__(self,
                 timeval=0,
                 index=u'',
                 chan=u'TOD',
                 refid=u'',
                 source=u'host'):
        self.index = index
        self.chan = chan
        self.refid = refid
        self.source = source
        self.timeval = _tv2dec(timeval)
        if self.timeval < 0 or self.timeval >= 86400:
            raise ValueError(u'Time of day value not in range [0, 86400)')

    def __str__(self):
        """Return a normalised tod string"""
        return str(self.__unicode__())

    def __unicode__(self):
        """Return a normalised tod string"""
        return u'{0: >5} {1: <3} {2} {3} {4}'.format(self.index, self.chan,
                                                     self.timestr(4),
                                                     self.refid, self.source)

    def __repr__(self):
        """Return object representation string"""
        return "{5}({0}, {1}, {2}, {3}, {4})".format(repr(self.timeval),
                                                     repr(self.index),
                                                     repr(self.chan),
                                                     repr(self.refid),
                                                     repr(self.source),
                                                     self.__class__.__name__)

    def truncate(self, places=4):
        """Return a new truncated time value"""
        return self.__class__(timeval=self.timeval.quantize(
            _QUANT[places], rounding=decimal.ROUND_FLOOR),
                              chan=u'TRUNC')

    def as_hours(self, places=0):
        """Return decimal value in hours, truncated to the desired places"""
        return (self.timeval / 3600).quantize(_QUANT[places],
                                              rounding=decimal.ROUND_FLOOR)

    def as_minutes(self, places=0):
        """Return decimal value in minutes, truncated to the desired places"""
        return (self.timeval / 60).quantize(_QUANT[places],
                                            rounding=decimal.ROUND_FLOOR)

    def as_seconds(self, places=0):
        """Return decimal value in seconds, truncated to the desired places"""
        return self.timeval.quantize(_QUANT[places],
                                     rounding=decimal.ROUND_FLOOR)

    def timestr(self, places=4, zeros=False, hoursep=u'h', minsep=u':'):
        """Return time string component of the tod, whitespace padded"""
        return u'{0: >{1}}{2}'.format(
            _dec2str(self.timeval, places, zeros, hoursep, minsep),
            _QUANT_TWID[places], _QUANT_PAD[places])

    def omstr(self, places=3, zeros=False, hoursep=u':', minsep=u':'):
        """Return a 12 digit omega style time string"""
        if places > 3:
            places = 3  # Hack to clamp to 12 dig
        return u'{0: >{1}}{2}'.format(
            _dec2str(self.timeval, places, zeros, hoursep, minsep),
            _QUANT_TWID[places], _QUANT_OPAD[places])

    def meridiem(self, mstr=None, secs=True):
        """Return a 12hour time of day string with meridiem"""
        ret = None
        med = u'\u2006am'
        # unwrap timeval into a single 24hr period
        tv = self.timeval
        if tv >= 86400:
            tv = tv % 86400
        elif tv < 0:
            tv = 86400 - (tv.copy_abs() % 86400)

        # determine meridiem and adjust for display
        if tv >= 43200:
            med = u'\u2006pm'
        if mstr is not None:
            med = mstr
        tv = tv % 43200
        if tv < 3600:  # 12am/12pm
            tv += 43200
        if secs:
            ret = _dec2str(tv, 0, hoursep=u':', minsep=u':') + med
        else:
            ret = _dec2hm(tv) + med
        return ret

    def rawtime(self, places=4, zeros=False, hoursep=u'h', minsep=u':'):
        """Return time string of tod as string, without padding"""
        return _dec2str(self.timeval, places, zeros, hoursep, minsep)

    def speedstr(self, dist=200):
        """Return average speed estimate string for the provided distance"""
        if self.timeval == 0:
            return u'---.- km/h'
        return u'{0:5.1f} km/h'.format(3.6 * float(dist) / float(self.timeval))

    def rawspeed(self, dist=200):
        """Return an average speed estimate string without unit"""
        if self.timeval == 0:
            return u'-.-'
        return u'{0:0.1f}'.format(3.6 * float(dist) / float(self.timeval))

    def __lt__(self, other):
        if isinstance(other, tod):
            return self.timeval < other.timeval
        else:
            return self.timeval < other

    def __le__(self, other):
        if isinstance(other, tod):
            return self.timeval <= other.timeval
        else:
            return self.timeval <= other

    def __eq__(self, other):
        if isinstance(other, tod):
            return self.timeval == other.timeval
        else:
            return self.timeval == other

    def __ne__(self, other):
        if isinstance(other, tod):
            return self.timeval != other.timeval
        else:
            return self.timeval != other

    def __gt__(self, other):
        if isinstance(other, tod):
            return self.timeval > other.timeval
        else:
            return self.timeval > other

    def __ge__(self, other):
        if isinstance(other, tod):
            return self.timeval >= other.timeval
        else:
            return self.timeval >= other

    def __sub__(self, other):
        """Compute time of day subtraction and return a NET tod object"""
        if type(other) is not tod:  # Subclass must override this method
            return NotImplemented
        if self.timeval >= other.timeval:
            oft = self.timeval - other.timeval
        else:
            oft = 86400 - other.timeval + self.timeval
        return tod(timeval=oft, chan=u'NET')

    def __add__(self, other):
        """Compute time of day addition and return a new tod object"""
        if type(other) is not tod:  # Subclass must override this method
            return NotImplemented
        return tod(timeval=(self.timeval + other.timeval) % 86400, chan=u'SUM')

    def __pos__(self):
        """Unary + operation"""
        return self.__class__(self.timeval, chan=u'POS')

    def __abs__(self):
        """Unary absolute value"""
        return self.__class__(self.timeval.copy_abs(), chan=u'ABS')


class agg(tod):
    """Aggregate time type"""

    def __init__(self,
                 timeval=0,
                 index=u'',
                 chan=u'AGG',
                 refid=u'',
                 source=u'host'):
        self.index = index
        self.chan = chan
        self.refid = refid
        self.source = source
        self.timeval = _tv2dec(timeval)

    def __add__(self, other):
        """Compute addition and return aggregate"""
        if isinstance(other, tod):
            return agg(timeval=self.timeval + other.timeval, chan=u'AGG')
        elif isinstance(other, (int, decimal.Decimal)):
            return agg(timeval=self.timeval + other, chan=u'AGG')
        else:
            return NotImplemented

    def __sub__(self, other):
        """Compute subtraction and return aggregate"""
        if isinstance(other, tod):
            return agg(timeval=self.timeval - other.timeval, chan=u'AGG')
        elif isinstance(other, (int, decimal.Decimal)):
            return agg(timeval=self.timeval - other, chan=u'AGG')
        else:
            return NotImplemented

    def __neg__(self):
        """Unary - operation"""
        return self.__class__(self.timeval.copy_negate(), chan=u'AGG')


# TOD constants
ZERO = tod()
ONE = tod(u'1.0')
MINUTE = tod(u'1:00')
MAX = tod(u'23h59:59.9999')  # largest val possible for tod
MAXELAP = tod(u'23h30:00')  # max displayed elapsed time

# Fake times for special cases
FAKETIMES = {
    u'catch': tod(ZERO, chan=u'catch'),
    u'w/o': tod(ZERO, chan=u'w/o'),
    u'max': tod(MAX, chan=u'max'),
    u'ntr': tod(MAX, chan=u'ntr'),
    u'caught': tod(MAX, chan=u'caught'),
    u'rel': tod(MAX, chan=u'rel'),
    u'abort': tod(MAX, chan=u'abort'),
    u'otl': tod(MAX, chan=u'otl'),
    u'dnf': tod(MAX, chan=u'dnf'),
    u'dns': tod(MAX, chan=u'dns'),
    u'dsq': tod(MAX, chan=u'dsq'),
}
_extra = decimal.Decimal(u'0.00001')
_cof = decimal.Decimal(u'0.00001')
for _c in [
        u'ntr', u'caught', u'rel', u'abort', u'otl', u'dnf', u'dns', u'dsq'
]:
    FAKETIMES[_c].timeval += _cof
    _cof += _extra


class todlist(object):
    """ToD list helper class for managing splits and ranks"""

    def __init__(self, lbl=u''):
        self.__label = lbl
        self.__store = []

    def __iter__(self):
        return self.__store.__iter__()

    def __len__(self):
        return len(self.__store)

    def __getitem__(self, key):
        return self.__store[key]

    def rank(self, bib, series=u''):
        """Return current 0-based rank for given bib"""
        ret = None
        count = 0
        i = 0
        lpri = None
        lsec = None
        for lt in self.__store:
            # scan times for updating ranks
            if lpri is not None:
                if lt[0] != lpri or lt[1] != lsec:
                    i = count
            # if rider matches, break
            if lt[0].refid == bib and lt[0].index == series:
                ret = i
                break
            lpri = lt[0]
            lsec = lt[1]
            count += 1
        return ret

    def clear(self):
        """Clear list"""
        self.__store = []
        return 0

    def remove(self, bib, series=u''):
        """Remove all times matching the supplied bib and optional series"""
        i = 0
        while i < len(self.__store):
            if (self.__store[i][0].refid == bib
                    and self.__store[i][0].index == series):
                del self.__store[i]
            else:
                i += 1
        return i

    def insert(self, pri=None, sec=None, bib=None, series=u'', prec=3):
        """Insert primary and secondary tod labeled with bib and series"""
        ret = None
        trunc = True
        if pri in FAKETIMES:  # re-assign a coded 'finish'
            pri = FAKETIMES[pri]
            trunc = False  # retain precision for primary comparison

        if isinstance(pri, tod):
            if bib is None:
                bib = pri.index
            if sec is None:
                sec = ZERO
            if trunc:
                pri = pri.truncate(prec)
                sec = sec.truncate(prec)
            rt0 = tod(pri, chan=self.__label, refid=bib, index=series)
            rt1 = tod(sec, chan=self.__label, refid=bib, index=series)
            ret = _bisect(self.__store, (rt0, rt1))
            self.__store.insert(ret, (rt0, rt1))
        return ret
