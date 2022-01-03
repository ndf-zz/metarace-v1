
"""Rider name database helper object.

This module provides a simple object helper for manipulating the
rider namebank database. Methods are provided for retrieving rows
by license no and for searching by name.

namebank objects are intended to be used in a 'with' context eg:

  with namebank() as n:
     matches = n.search(u'first', u'last')

NOTE: namebank keys are strictly ASCII, all other fields are unicode.

"""

import os
import shelve
import datetime
import metarace
from metarace import ucsv

COL_ID = 0
COL_FIRST = 1
COL_LAST = 2
COL_CLUB = 3
COL_CAT = 4
COL_STATE = 5	## origin
COL_DOB = 6
COL_GENDER = 7
COL_NATION = 8	# UCI Nationality as per 1.1.033
COL_EXPIRE = 9
COL_PARA = 10
COL_UCI = 11 # UCI ID as string

THISYEAR = datetime.date.today().year	# today returns local date

def get_ioc_codes():
    """Load and return a map of IOC codes->Countries."""
    iocmap = {}
    with open (metarace.default_file(u'IOC_Codes.csv'), 'rb') as f:
        cr = ucsv.UnicodeReader(f)
        for r in cr:
            if len(r) > 1 and u'CODE' not in r[0].upper():
                iocmap[r[0]] = r[1]
    return iocmap

def bgswitch(gender):
    ret = u'B'
    if gender.lower() == u'w':
        ret = u'G'
    return ret

def dob2lcat(gender,dob,reg=u'opn',para=None,year=None):
    """Return license category."""
    ty = THISYEAR
    if year is not None:
        ty = year
    lcat = u'U/C'
    if u'mas' in reg.lower():
        reg = u'mas'
    elif reg.lower()==u'n/c':
        return u'N/C'   # non-racing membership
    else:
        reg = u'opn'
    yob = dob.split(u'-')[0]
    if para:
        # para cycling is weird
        pcat = para
        if u'B' in para:
            pcat = u'B'
        lcat = gender + pcat
    elif yob.isdigit() and len(yob) == 4:
        age = ty - int(yob)
        if age < 13:
            gender = bgswitch(gender)
        if age < 11:
            lcat = u'J' + gender + u'11'
        elif age < 13:
            lcat = u'J' + gender + u'13'
        elif age < 15:
            lcat = u'J' + gender + u'15'
        elif age < 17:
            lcat = u'J' + gender + u'17'
        elif age < 19:
            lcat = u'J' + gender
        elif gender != u'W' and age < 23:
            lcat = gender + u'23'
        else:
            if reg == u'mas':   # Masters license
                mcat = u''
                # determine age cat
                if age < 35:
                    mcat = u'1'
                elif age < 40:
                    mcat = u'2'
                elif age < 45:
                    mcat = u'3'
                elif age < 50:
                    mcat = u'4'
                elif age < 55:
                    mcat = u'5'
                elif age < 60:
                    mcat = u'6'
                elif age < 65:
                    mcat = u'7'
                elif age < 70:
                    mcat = u'8'
                elif age < 75:
                    mcat = u'9'
                else:
                    mcat = u'10'
                lcat = gender + u'MAS' + mcat
            else:
                lcat = u'Elite' + gender
    else:
        lcat = None
    return lcat

def search_name(namestr):
    return namestr.strip().lower().encode(u'ascii',u'ignore')

class namebank(object):
    """Namebank storage and search module.

    The namebank object maintains a persistent storage of rider rows
    with the following structure:

     KEY - String: CA license 'no' or rider ID
     VAL - Array: [as per COL*]

    Searching by name uses an internal index to facilitate speedy
    return of matching riders.

    Internally two python shelve objects are used to map search keys
    to lists of rider info (for the namebank) or rider ids (for the
    name index).

    """
    def __init__(self):
        """Constructor."""
        self.__open = False
        self.__nb = None
        self.__ind = None

    def open(self):
        """(Re)Open the namebank database files."""
        self.close()
        self.__nb = shelve.open(os.path.join(metarace.DEFAULTS_PATH,
                                             u'namebank'))
        self.__ind = shelve.open(os.path.join(metarace.DEFAULTS_PATH,
                                              u'nameindx'))
        self.__open = True

    def close(self):
        """Close the namebank database files."""
        if self.__nb is not None:
            self.__nb.close()
            self.__nb = None
        if self.__ind is not None:
            self.__ind.close()
            self.__ind = None
        self.__open = False

    def raw_search(self, skey):
        """Return an exact match from the index."""
        ret = None
        if skey in self.__ind:
            ret = [a for a in self.__ind[skey]]
        return ret

    def ksearch(self, nkey=u''):
        """Return riders matching a NRS key search."""
        # NRS Key: flllyyyy[n*]
        # where:
        #        f = lowercase first initial
        #        lll = lowercase 3 ascii from surname not unicode/punc
        #        yyyy = year of birth
        #        n* = disambiguation digits, ignored in search
        key = unicode(search_name(nkey)).ljust(12)
        ret = set()
        fs = key[0]
        ls = key[1:4]
        yob = key[4:8]
        da = key[8:]
        cl = self.search(fs, ls)
        for ri in cl:
            dyo = self.__nb[ri][COL_DOB][0:4]
            if dyo == yob:
                ret.add(ri)

        return ret

    def search(self, first=u'', last=u'',dob=None):
        """Return a set of matching rider ids from the namebank."""

        # reformat search strings
        fs = search_name(first)
        ls = search_name(last)

        # Build candidate id set
        cset = set()
        if fs[0:4] in self.__ind:
            cset = cset.union(self.__ind[fs[0:4]])
        if ls[0:4] in self.__ind:
            cset = cset.union(self.__ind[ls[0:4]])
        # filter candidates further on full search string
        fset = set()
        if len(first) > 0:
            for r in cset:
                doc = self.__nb[r][COL_DOB]
                fn = self.__nb[r][COL_FIRST]
                if search_name(fn).find(fs) == 0:
                    fset.add(r)	# mark r in first name set
        else:
            fset = cset		# 'empty' first matches all
        lset = set()
        if len(last) > 0:
            for r in cset:
                doc = self.__nb[r][COL_DOB]
                ln = self.__nb[r][COL_LAST]
                if search_name(ln).find(ls) == 0:
                    lset.add(r)	# mark r in last name set
        else:
            lset = cset

        # return intersection of fset and lset
        return fset.intersection(lset)

    def __iter__(self):
        """Iterate over all namebank records."""
        for key in self.__nb:
            yield(self.__nb[key])

    def __len__(self):
        """Called to implement the built-in function len()."""
        return len(self.__nb)

    def __getitem__(self, key):
        """Called to implement evaluation of self[key]."""
        return self.__nb[key]

    def __contains__(self, key):
        """Called to implement membership test operators."""
        return key in self.__nb

    def __enter__(self):
        """Enter the runtime context related to this object."""
        if not self.__open:
            self.open()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        """Exit the runtime context related to this object."""
        self.close()
