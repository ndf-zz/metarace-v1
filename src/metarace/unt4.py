
"""UNT4 Packet Wrapper."""

# Mode 1 constants
NUL = 0x00
SOH = 0x01
STX = 0x02
EOT = 0x04
HOME= 0x08
CR  = 0x0d
LF  = 0x0a
ERL = 0x0b
ERP = 0x0c
DLE = 0x10
DC2 = 0x12
DC3 = 0x13
DC4 = 0x14
US = 0x1f

# String encodings for Mode 1 constants over telegraph
ENCMAP = {
  unichr(NUL):u'<0>',
  unichr(SOH):u'<O>',
  unichr(STX):u'<T>',
  unichr(EOT):u'<E>',
  unichr(CR):u'<R>',
  unichr(LF):u'<A>',
  unichr(ERL):u'<L>',
  unichr(ERP):u'<P>',
  unichr(DLE):u'<D>',
  unichr(DC2):u'<2>',
  unichr(DC3):u'<3>',
  unichr(DC4):u'<4>',
  unichr(US):u'<U>',
}
  
# Translation map to blank forbidden control characters in header & text
TRANSMAP = {
  SOH:u' ',
  STX:u' ',
  EOT:u' ',
  ERL:u' ',
  ERP:u' ',
  DLE:u' ',
  DC2:u' ',
  DC3:u' ',
  DC4:u' ',
}

def encode(ubuf=u''):
    """Encode the unt4 buffer for use over telegraph."""
    ubuf = ubuf.replace(u'<', u'<>')
    for key in ENCMAP:
        ubuf = ubuf.replace(key, ENCMAP[key])
    return ubuf

def decode(tbuf=u''):
    """Decode the telegraph buffer to unt4 pack."""
    for key in ENCMAP:
        tbuf = tbuf.replace(ENCMAP[key], key)
    tbuf = tbuf.replace(u'<>', u'<')
    return tbuf

class unt4(object):
    """UNT4 Packet."""
    def __init__(self, unt4str=None, 
                   prefix=None, header=u'', 
                   erp=False, erl=False, 
                   xx=None, yy=None, text=u''):
        """Constructor.

        Parameters:

          unt4str -- packed unt4 string, overrides other params
          prefix -- prefix byte <DC2>, <DC3>, etc
          header -- header string eg 'R_F$'
          erp -- true for general clearing <ERP>
          erl -- true for <ERL>
          xx -- packet's column offset 0-99
          yy -- packet's row offset 0-99
          text -- packet content string

        """
        self.prefix = prefix    # <DC2>, <DC3>, etc
        self.header = header.translate(TRANSMAP)
        self.erp = erp          # true for general clearing <ERP>
        self.erl = erl          # true for <ERL>
        self.xx = xx            # input column 0-99
        self.yy = yy            # input row 0-99
        self.text = text.translate(TRANSMAP)
        if unt4str is not None:
            self.unpack(unt4str)

    def unpack(self, unt4str=u''):
        """Unpack the UNT4 unicode string into this object."""
        if len(unt4str) > 2 and unt4str[0] == unichr(SOH) \
                            and unt4str[-1] == unichr(EOT):
            self.prefix = None
            newhead = u''
            newtext = u''
            self.erl = False
            self.erp = False
            head = True		# All text before STX is considered header
            stx = False
            dle = False
            dlebuf = u''
            i = 1
            packlen = len(unt4str) - 1
            while i < packlen:
                och = ord(unt4str[i])
                if och == STX:
                    stx = True
                    head = False
                elif och == DLE and stx:
                    dle = True
                elif dle:
                    dlebuf += unt4str[i]
                    if len(dlebuf) == 4:
                        dle = False
                elif head:
                    if och in (DC2, DC3, DC4):
                        self.prefix = och   # assume pfx before head text
                    else:
                        newhead += unt4str[i]
                elif stx:
                    if och == ERL:
                        self.erl = True
                    elif och == ERP:
                        self.erp = True
                    else:
                        newtext += unt4str[i]
                i += 1
            if len(dlebuf) == 4:
                self.xx = int(dlebuf[:2])
                self.yy = int(dlebuf[2:])
            self.header = newhead
            self.text = newtext

    def pack(self):
        """Return Omega Style UNT4 unicode string packet."""
        head = u''
        text = u''
        if self.erp:	# overrides any other message content
            text = unichr(STX) + unichr(ERP)
        else:
            head = self.header
            if self.prefix is not None:
                head = unichr(self.prefix) + head
            if self.xx is not None and self.yy is not None:
                text += unichr(DLE) + u'{0:02d}{1:02d}'.format(self.xx, self.yy)
            if self.text:
                text += self.text
            if self.erl:
                text += unichr(ERL)
            if len(text) > 0:
                text = unichr(STX) + text
        return unichr(SOH) + head + text + unichr(EOT)
 
# Pre-defined messages
GENERAL_CLEARING = unt4(erp=True)
GENERAL_EMPTY = unt4(xx=0,yy=0,text=u'')
OVERLAY_ON = unt4(header=u'OVERLAY ON')
OVERLAY_OFF = unt4(header=u'OVERLAY OFF')
OVERLAY_CLOCK = unt4(header=u'OVERLAY 01')
OVERLAY_MATRIX = unt4(header=u'OVERLAY 00')
OVERLAY_IMAGE = unt4(header=u'OVERLAY 02')
OVERLAY_BLANK = unt4(header=u'OVERLAY 03')
OVERLAY_GBLANK = unt4(header=u'overlay', text=u'0')
OVERLAY_GTITLE = unt4(header=u'overlay', text=u'2')
OVERLAY_GMATRIX = unt4(header=u'overlay', text=u'1')
