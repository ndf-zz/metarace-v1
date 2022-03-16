"""HTML output library.

Cheap and nasty functional primitives for HTML output. Each primitive
returns a single string. No checking is performed on the structure of
the document produced. All elements take a named parameter 'attrs'
which is a dict of key/value attributes. Non-empty elements take a
parameter 'clist' which is a list of other constructed elements.

Note: "input" is not provided due to conflict with python2 name.

Example for an empty element:

    hr(attrs={'id':'thehr'}) => <hr id="thehr">

Example for an element with content:

    a(['link text'], attrs={'href':'#target'}) => 

	<a href="#target">link text</a>

Example paragraph:

    p(['Check the',
       a(['website'], attrs={'href':'#website'}),
       'for more.']) => 

	<p>Check the\n<a href="#website">website</a>\nfor more.</p>

"""

from xml.sax.saxutils import escape, quoteattr
import sys


def html(headlist=[], bodylist=[]):
    """Emit HTML document."""
    return u'\n'.join([
        preamble(), u'<html lang="en">',
        head(headlist),
        body(bodylist), u'</html>'
    ])


def preamble():
    """Emit HTML preamble."""
    return u'<!doctype html>'


def attrlist(attrs):
    """Convert attr dict into properly escaped attrlist."""
    alist = []
    for a in attrs:
        alist.append(a.lower() + u'=' + quoteattr(attrs[a]))
    if len(alist) > 0:
        alist.insert(0, u'')
        return u' '.join(alist)
    else:
        return u''


def escapetext(text=u''):
    """Return escaped copy of text."""
    return escape(text, {u'"': u'&quot;'})


def comment(commentstr=u''):
    """Insert comment."""
    return u'<!-- ' + commentstr.replace(u'--', u'') + u' -->'


# Declare all the empty types
for empty in [
        u'meta', u'link', u'base', u'param', u'hr', u'br', u'img', u'col'
]:

    def emptyfunc(attrs={}, elem=empty):
        return u'<' + elem + attrlist(attrs) + u'>'

    setattr(sys.modules[__name__], empty, emptyfunc)

# Declare all the non-empties
for nonempty in [
        u'head',
        u'body',
        u'title',
        u'div',
        u'nav',
        u'style',
        u'script',
        u'p',
        u'h1',
        u'h2',
        u'h3',
        u'h4',
        u'h5',
        u'h6',
        u'ul',
        u'ol',
        u'li',
        u'dl',
        u'dt',
        u'dd',
        u'address',
        u'pre',
        u'blockquote',
        u'a',
        u'span',
        u'em',
        u'strong',
        u'dfn',
        u'code',
        u'samp',
        u'kbd',
        u'var',
        u'cite',
        u'abbr',
        u'acronym',
        u'q',
        u'sub',
        u'sup',
        u'tt',
        u'i',
        u'big',
        u'small',
        u'label',
        u'form',
        u'select',
        u'optgroup',
        u'option',
        u'textarea',
        u'fieldset',
        u'legend',
        u'button',
        u'table',
        u'caption',
        u'thead',
        u'tfoot',
        u'tbody',
        u'colgroup',
        u'tr',
        u'th',
        u'td',
]:

    def nonemptyfunc(clist=[], attrs={}, elem=nonempty):
        if isinstance(clist, (
                str,
                unicode,
        )):
            clist = [clist]
        return (u'<' + elem + attrlist(attrs) + u'>' + u'\n'.join(clist) +
                u'</' + elem + u'>')

    setattr(sys.modules[__name__], nonempty, nonemptyfunc)


# output a valid but empty html templatye
def emptypage():
    return html([
        meta(attrs={u'charset': u'utf-8'}),
        meta(
            attrs={
                u'name': u'viewport',
                u'content': u'width=device-width, initial-scale=1'
            }),
        title(u'__REPORT_TITLE__'),
        link(
            attrs={
                u'href':
                u'https://cdn.jsdelivr.net/npm/bootstrap@5.0.0-beta3/dist/css/bootstrap.min.css',
                u'integrity':
                u'sha384-eOJMYsd53ii+scO/bJGFsiCZc+5NDVN2yr8+0RDqr0Ql0h+rP48ckxlpbzKgwra6',
                u'crossorigin': u'anonymous',
                u'rel': u'stylesheet'
            }),
        link(
            attrs={
                u'href':
                u'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.4.0/font/bootstrap-icons.css',
                u'rel': u'stylesheet'
            }),
    ], [
        u'__REPORT_NAV__',
        div([
            h1(u'__REPORT_TITLE__'),
            u'\n',
            comment(u'Begin report content'),
            u'__REPORT_CONTENT__',
            comment(u'End report content'),
            u'\n',
        ],
            attrs={u'class': u'container'})
    ])
