
"""String filtering, truncation and padding."""

# Note: These functions consider unicode string length and
#       displayed string length to be equal, so any string with zero
#	length characters (eg combining) will be incorrectly
#	truncated and/or padded. Output to fixed-width displays
#	like DHI and track announce will be incorrect.

import re
from random import randint

# replace codepoints 0->255 with space unless overridden
# "protective" against unencoded ascii strings and control chars
SPACEBLOCK = u''
for i in xrange(0,256):
    SPACEBLOCK += unichr(i)

# unicode translation 'map' class
class unicodetrans(object):
  def __init__(self, keep=u'', replace=SPACEBLOCK, replacechar=u' '):
    self.comp = dict((ord(c),replacechar) for c in replace)
    for c in keep:
        self.comp[ord(c)] = c
  def __getitem__(self, k):	# override to return a None
    return self.comp.get(k)

INTEGER_UTRANS = unicodetrans(u'-0123456789')
NUMERIC_UTRANS = unicodetrans(u'-0123456789.e')
PLACELIST_UTRANS = unicodetrans(
u'-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
PLACESERLIST_UTRANS = unicodetrans(
u'-.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
BIBLIST_UTRANS = unicodetrans(
u'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
RIDERNO_UTRANS = unicodetrans(
u'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',u'',u'')
BIBSERLIST_UTRANS = unicodetrans(
u'.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
WEBFILE_UTRANS = unicodetrans(
u'_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',u'.',u'_')
# special case: map controls and spaces, but keep everything else
PRINT_UTRANS = {}
for cp in xrange(0,0x20):
    PRINT_UTRANS[cp] = u' '
for cp in xrange(0x7f,0xa1):
    PRINT_UTRANS[cp] = u' '
for cp in xrange(0x2000,0x200B):
    PRINT_UTRANS[cp] = u' '
PRINT_UTRANS[0x1680] = u' '
PRINT_UTRANS[0x180e] = u' '
PRINT_UTRANS[0x202f] = u' '
PRINT_UTRANS[0x205f] = u' '
PRINT_UTRANS[0x3000] = u' '
PRINT_UTRANS[0xffa0] = u' '

# timing channels - this duplicates defs in timy
CHAN_START = 0
CHAN_INT = 9
CHAN_UNKNOWN = -1

# running number comparisons
RUNNER_NOS = {
 u'red': 0,
 u'whi': 1,
 u'blu': 2,
 u'yel': 3,
 u'grn': 4,
 u'pin': 5,
 u'bla': 6,
 u'gry': 7,
 u'ora': 8,
 u'pur': 9,
 u'rdw': 10,
 u'blw': 11,
 u'ylw': 12,
 u'grw': 13
}

DNFCODEMAP = { u'hd': 0,
               u'dsq': 1,
               u'dnf': 3,
               u'dns': 4,
               u'': 2}

def cmp_dnf(x, y):
    """Comparison func for two dnf codes."""
    if x not in DNFCODEMAP:
        x = u''
    if y not in DNFCODEMAP:
        y = u''
    return cmp(DNFCODEMAP[x], DNFCODEMAP[y])
    
def riderno_key(bib):
    """Return a comparison key for sorting rider number strings."""
    return bibstr_key(bib)

def dnfcode_key(code):
    """Return a rank/dnf code sorting key."""
    # rank [rel] '' dsq hd|otl dnf dns
    dnfordmap = {
                 u'rel':8000,
                 u'':8500,
                 u'hd':8800,u'otl':8800,
                 u'dnf':9000,
                 u'dns':9500,
                 u'dsq':10000,}
    ret = 0
    if code is not None:
        code = code.lower()
        if code in dnfordmap:
            ret = dnfordmap[code]
        else:
            code = code.strip(u'.')
            if code.isdigit():
                ret = int(code)
    return ret

def bibstr_key(bibstr=u''):
    """Return a comparison key for sorting rider bib.ser strings."""
    (bib, ser) = bibstr2bibser(bibstr)
    bval = 0
    if bib.isdigit():
        bval = int(bib)
    else:
        sbib = bib.translate(INTEGER_UTRANS).strip()
        if sbib and sbib.isdigit():
            bval = int(sbib)
        else:
            if bib.lower()[0:3] in RUNNER_NOS:
                bval = RUNNER_NOS[bib.lower()[0:3]]
            else:
                bval = id(bib)
    sval = 0
    if ser != u'':
        sval = ord(ser[0])<<12
    return sval | (bval&0xfff)

def randstr():
    """Return a string of random digits."""
    return unicode(randint(10000,99999))

def promptstr(prompt=u'', value=u''):
    """Prefix a non-empty string with a prompt, or return empty."""
    ret = u''
    if value:
        ret = prompt + u' ' + value
    return ret

def listsplit(liststr=u''):
    """Return a split and stripped list."""
    ret = []
    for e in liststr.split(u','):
        ret.append(e.strip())
    return ret

def heatsplit(heatstr):
    """Return a failsafe heat/lane pair for the supplied heat string."""
    hv = heatstr.split(u'.')
    while len(hv) < 2:
        hv.append(u'0')
    return(riderno_key(hv[0]), riderno_key(hv[1]))
    
def fitname(first, last, width, trunc=False):
    """Return a truncated name field for fixed-width display.

    Attempts to modify name to fit within width as follows:

    'First Lastone-Lasttwo'

	1: Split lastname ->	'First Lasttwo'
	2: Abbreviate first -> 	'F. Lastone-Lasttwo'
	3: Both ->		'F. Lasttwo'

    If trunc is set, truncate and replace final char with
    ellipsis u'\u2026':		'F. Lastt...'
    """
    ret = u''
    fstr = first.strip().title()
    lstr = last.strip().upper()
    if len(fstr) + len(lstr) >= width:
        lshrt = lstr.split(u'-')[-1].strip()
        if len(fstr) + len(lshrt) >= width:
            fshrt = fstr
            if fstr and len(fstr) > 1:
                fshrt = fstr[0] + u'.'
            if len(fshrt) + len(lstr) >= width:
                lstr = lshrt
                if len(fstr) + len(lshrt) >= width:
                    # Abbrev first with split last
                    fstr = fshrt
                else:
                    # Full first with split last
                    pass
            else:
                # Abbrev first with full last
                fstr = fshrt
        else:
            # Full first with split last
            lstr = lshrt
    ret = u' '.join([fstr, lstr]).strip()
    if trunc and len(ret) > width:
        if width > 4:
            ret = ret[0:(width - 1)] + u'\u2026' # Ellipsis
        else:
            ret = ret[0:width]
    return ret

def drawno_encirc(drawstr=u''):
    ret = drawstr
    if drawstr.isdigit():
        try:
            ival = int(drawstr)
            if ival > 0 and ival <= 10:
                ret = (u'\u00a0' + 	# nbsp to get full line height
                       unichr(0x245f + ival)) # CP U+2460 "Circled digit"
        except Exception:
            pass
    return ret

def num2ord(place):
    """Return ordinal for the given place."""
    omap = { u'1' : u'st',
             u'2' : u'nd',
             u'3' : u'rd',
             u'11' : u'th',
             u'12' : u'th',
             u'13' : u'th' }
    if place in omap:
        return place + omap[place]
    elif place.isdigit():
        if len(place) > 1 and place[-1] in omap: # last digit 1,2,3
            return place + omap[place[-1]]
        else:
            return place + u'th'
    else:
        return place

def rank2int(rank):
    """Convert a rank/placing string into an integer."""
    ret = None
    try:
        ret = int(rank.replace(u'.',u''))
    except Exception:
        pass
    return ret

def mark2int(handicap):
    """Convert a handicap string into an integer number of metres."""
    handicap = handicap.strip().lower()
    ret = None				# not recognised as handicap
    if handicap != u'':
        if handicap[0:3] == u'scr':		# 'scr{atch}'
            ret = 0
        else:				# try [number]m form
           handicap = handicap.translate(INTEGER_UTRANS).strip()
           try:
               ret = int(handicap)
           except Exception:
               pass
    return ret
       
def truncpad(srcline, length, align=u'l', ellipsis=True):
    """Return srcline truncated and padded to length, aligned as requested."""
    # truncate
    if len(srcline) > length:
        if ellipsis and length > 4:
            ret = srcline[0:(length-1)] + u'\u2026' # Ellipsis
        else:
            ret = srcline[0:length]
    else:
        # pad
        if len(srcline) < length:
            if align == u'l':
                ret = srcline.ljust(length)
            elif align == u'r':
                ret = srcline.rjust(length)
            else:
                ret = srcline.center(length)
        else:
            ret = srcline
    return ret

def resname_bib(bib, first, last, club):
    """Return rider name formatted for results with bib."""
    ret = [bib, u' ', fitname(first, last, 64)]
    if club is not None and club != u'':
        if len(club) < 4:
            club=club.upper()
        ret.extend([u' (', club, u')'])
    return u''.join(ret)

def resname(first, last=None, club=None):
    """Return rider name formatted for results."""
    ret = fitname(first, last, 64)
    if club is not None and club != u'':
        if len(club) < 4:
            club=club.upper()
        ret = u''.join([ret, u' (', club, u')'])
    return ret

def listname(first, last=None, club=None):
    """Return a rider name summary field for non-edit lists."""
    ret = fitname(first, last, 32)
    if club:
        if len(club) < 4:
            club=club.upper()
        ret = u''.join([ret, u' (', club, u')'])
    return ret

def reformat_bibserlist(bibserstr):
    """Filter and return a bib.ser start list."""
    return u' '.join(bibserstr.translate(BIBSERLIST_UTRANS).split())

def reformat_bibserplacelist(placestr):
    """Filter and return a canonically formatted bib.ser place list."""
    if u'-' not in placestr:		# This is the 'normal' case!
        return reformat_bibserlist(placestr)
    # otherwise, do the hard substitutions...
    placestr = placestr.translate(PLACESERLIST_UTRANS).strip()
    placestr = re.sub(r'\s*\-\s*', r'-', placestr)	# remove surrounds
    placestr = re.sub(r'\-+', r'-', placestr)		# combine dupes
    return u' '.join(placestr.strip(u'-').split())

def reformat_biblist(bibstr):
    """Filter and return a canonically formatted start list."""
    return u' '.join(bibstr.translate(BIBLIST_UTRANS).split())

## TODO: remove def reformat_riderlist(riderstr, rdb=None, series=u''):

def riderlist_split(riderstr, rdb=None, series=u''):
    """Filter, search and return a list of matching riders for entry."""
    ret = []
    riderstr = riderstr.lower()

    # special case: 'all' -> return all riders from the specified series.
    if rdb is not None and riderstr.strip() == u'all':
        riderstr = u''
        for r in rdb:
            if r[5] == series:
                ret.append(r[0])
    
    # pass 1: search for categories
    if rdb is not None:
        for cat in sorted(rdb.listcats(series), key=len, reverse=True):
            if len(cat) > 0 and cat.lower() in riderstr:
                ret.extend(rdb.biblistfromcat(cat, series))
                riderstr = riderstr.replace(cat.lower(), u'')

    # pass 2: append riders and expand any series if possible
    riderstr = reformat_placelist(riderstr)
    for nr in riderstr.split():
        if u'-' in nr:
            # try for a range...
            l = None
            n = None
            for r in nr.split(u'-'):
                if l is not None:
                    if l.isdigit() and r.isdigit():
                        start = int(l)
                        end = int(r)
                        if start < end:
                            c = start
                            while c < end:
                                ret.append(unicode(c))
                                c += 1
                        else:
                            ret.append(l)
                    else:
                        # one or both not ints
                        ret.append(l)
                else:
                    pass
                l = r
            if l is not None: # catch final value
                ret.append(l)
        else:
            ret.append(nr)
    return ret

def placeset(spec=u''):
    """Convert a place spec into an ordered set of place ints."""

    # NOTE: ordering of the set must be retained to correctly handle
    #       autospecs where the order of the places is not increasing
    #       eg: sprint semi -> sprint final, the auto spec is: 3,1,2,4
    #       so the 'winners' go to the gold final and the losers to the
    #       bronze final.
    ret = []
    spec = reformat_placelist(spec)
    # pass 1: expand ranges
    for nr in spec.split():
        if u'-' in spec:
            # try for a range...
            l = None
            n = None
            for r in nr.split(u'-'):
                if l is not None:
                    if l.isdigit() and r.isdigit():
                        start = int(l)
                        end = int(r)
                        if start < end:
                            c = start
                            while c < end:
                                ret.append(unicode(c))
                                c += 1
                        else:
                            ret.append(l)	# give up on last val
                    else:
                        # one or both not ints
                        ret.append(l)
                else:
                    pass
                l = r
            if l is not None: # catch final value
                ret.append(l)
        else:
            ret.append(nr)
    # pass 2: filter out non-numbers, only places considered
    rset = []
    for i in ret:
        if i.isdigit():
            ival = int(i)
            if ival not in rset:
                rset.append(ival)
    return rset

def reformat_placelist(placestr):
    """Filter and return a canonically formatted place list."""
    if u'-' not in placestr:
        return reformat_biblist(placestr)
    # otherwise, do the hard substitutions...
    placestr = placestr.translate(PLACELIST_UTRANS).strip()
    placestr = re.sub(r'\s*\-\s*', r'-', placestr)	# remove surrounds
    placestr = re.sub(r'\-+', r'-', placestr)		# combine dupes
    return u' '.join(placestr.strip(u'-').split())

def confopt_bool(confstr):
    """Check and return a boolean option from config."""
    if isinstance(confstr, basestring):
        if confstr.lower() in [u'yes', u'true', u'1']:
            return True
        else:
            return False
    else:
        return bool(confstr)

def plural(count=0):
    """Return plural extension for provided count."""
    ret = u's'
    if count == 1:
        ret = u''
    return ret

def confopt_riderno(confstr, default=u''):
    """Check and return rider number, filtered only."""
    return confstr.translate(RIDERNO_UTRANS).strip()

def confopt_float(confstr, default=None):
    """Check and return a floating point number."""
    ret = default
    try:
        ret = float(confstr)
    except Exception:
        pass
    return ret

def confopt_distunits(confstr):
    """Check and return a valid unit from metres or laps."""
    if u'lap' in confstr.lower():
        return u'laps'
    else:
        return u'metres' 

def confopt_int(confstr, default=None):
    """Check and return a valid integer."""
    ret = default
    try:
        ret = int(confstr)
    except Exception:
        pass
    return ret

def confopt_posint(confstr, default=None):
    """Check and return a valid positive integer."""
    ret = default
    try:
        ret = int(confstr)
        if ret < 0:
            ret = default
    except Exception:
        pass
    return ret

def confopt_dist(confstr, default=None):
    """Check and return a valid distance unit."""
    return confopt_posint(confstr, default)

def chan2id(chanstr=u'0'):
    """Return a channel ID for the provided string, without fail."""
    ret = CHAN_UNKNOWN
    if (isinstance(chanstr, basestring) and len(chanstr) > 1
        and chanstr[0] == u'C' and chanstr[1].isdigit()):
        ret = int(chanstr[1])
    else:
        try:
            ret = int(chanstr)
        except Exception:
            pass
    if ret < CHAN_UNKNOWN or ret > CHAN_INT:
        ret = CHAN_UNKNOWN
    return ret

def id2chan(chanid=0):
    """Return a normalised channel string for the provided channel id."""
    ret = u'C?'
    if isinstance(chanid, int) and chanid >= CHAN_START and chanid <= CHAN_INT:
        ret = u'C' + unicode(chanid)
    return ret

def confopt_chan(confstr, default=None):
    """Check and return a valid timing channel id string."""
    ret = chan2id(confstr)
    if ret == CHAN_UNKNOWN:
        ret = chan2id(default)
    return ret

def confopt_pair(confstr, value, default=None):
    """Return value or the default."""
    ret = default
    if confstr.lower() == value.lower():
        ret = value
    return ret

def confopt_list(confstr, list=[], default=None):
    """Return an element from list or default."""
    ret = default
    for elem in list:
        if confstr.lower() == elem.lower():
            ret = elem
            break
    return ret

def bibstr2bibser(bibstr=u''):
    """Split a bib.series string and return bib and series."""
    a = bibstr.strip().split(u'.')
    ret_bib = u''
    ret_ser = u''
    if len(a) > 0:
        ret_bib = a[0]
    if len(a) > 1:
        ret_ser = a[1]
    return (ret_bib, ret_ser)

def lapstring(lapcount=None):
    lapstr = u''
    if lapcount:
        lapstr = unicode(lapcount) + u' Lap'
        if lapcount > 1:
            lapstr += u's'
    return lapstr

def bibser2bibstr(bib=u'', ser=u''):
    """Return a valid bib.series string."""
    ret = bib
    if ser != u'':
        ret += u'.' + ser
    return ret

def titlesplit(src=u'', linelen=24):
    """Split a string on word boundaries to try and fit into 3 fixed lines."""
    ret = [u'', u'', u'']
    words = src.split()
    wlen = len(words)
    if wlen > 0:
        line = 0
        ret[line] = words.pop(0)
        for word in words:
            pos = len(ret[line])
            if pos + len(word) >= linelen:
                # new line
                line += 1
                if line > 2:
                    break
                ret[line] = word
            else:
                ret[line] += u' ' + word
    return ret
