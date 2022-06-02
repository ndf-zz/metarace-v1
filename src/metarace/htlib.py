"""HTML output library.

Cheap and nasty functional primitives for HTML output. Each primitive
returns a single string. No checking is performed on the structure of
the document produced. All elements take a named parameter 'attrs'
which is a dict of key/value attributes. Non-empty elements take a
parameter 'clist' which is a list of other constructed elements.

Note: <input> is provided by forminput()

Example for an empty element:

    hr(attrs={'id':'thehr'}) => <hr id="thehr">

Example for an element with content:

    a(['link text'], attrs={'href':'#target'}) => 

	<a href="#target">link text</a>

Example paragraph:

    p(('Check the',
       a(('website'), attrs={'href':'#website'}),
       'for more.')) => 

	<p>Check the\n<a href="#website">website</a>\nfor more.</p>

"""

from xml.sax.saxutils import escape, quoteattr
import sys


def html(headlist=(), bodylist=()):
    """Emit HTML document."""
    return u'\n'.join((preamble(), u'<html lang="en">', head(headlist),
                       body(bodylist, {'onload': 'ud();'}), u'</html>'))


def preamble():
    """Emit HTML preamble."""
    return u'<!doctype html>'


def attrlist(attrs):
    """Convert attr dict into escaped attrlist."""
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


# output a valid but empty html templatye
def emptypage():
    return html((
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
                u'https://cdn.jsdelivr.net/npm/bootstrap@5.0.2/dist/css/bootstrap.min.css',
                u'integrity':
                u'sha384-EVSTQN3/azprG1Anm3QDgpJLIm9Nao0Yz1ztcQTwFspd3yD65VohhpuuCOmLASjC',
                u'crossorigin': u'anonymous',
                u'rel': u'stylesheet'
            }),
        link(
            attrs={
                u'href':
                u'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.8.3/font/bootstrap-icons.css',
                u'rel': u'stylesheet'
            }),
        script((
            'function ud(){null!==document.querySelector("#pgre")&&setTimeout("history.go(0);",55329)}function rl(){return setTimeout("history.go(0);",10),!1}',
        )),
    ), (u'__REPORT_NAV__',
        div((
            h1(u'__REPORT_TITLE__'),
            u'__REPORT_CONTENT__',
        ),
            attrs={u'class': u'container'})))


# Declare all the empty types
for empty in (u'meta', u'link', u'base', u'param', u'hr', u'br', u'img',
              u'col'):

    def emptyfunc(attrs={}, tag=empty):
        return u'<' + tag + attrlist(attrs) + u'>'

    setattr(sys.modules[__name__], empty, emptyfunc)


def emptyfunc(attrs={}):
    return u'<input' + attrlist(attrs) + u'>'


setattr(sys.modules[__name__], 'forminput', emptyfunc)

# Declare all the non-empties
for nonempty in (
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
):

    def nonemptyfunc(clist=(), attrs={}, elem=nonempty):
        if isinstance(clist, basestring):
            clist = (clist, )
        return (u'<' + elem + attrlist(attrs) + u'>' + u'\n'.join(clist) +
                u'</' + elem + u'>')

    setattr(sys.modules[__name__], nonempty, nonemptyfunc)
