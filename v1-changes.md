# Changes from Version 1.11.3

The last official v1 release of metarace is 1.11.3, available
on [PyPI](https://pypi.org/project/metarace/). Below is a list
of changes moving from the 1.11.3 release to this repository.


## Base Library (metarace)

  - Change license from GPL to MIT and update headers
  - All ancillary programs are removed from library, only three main top levels are retained: metarace, trackmeet & roadrace
  - remove times7 wheeltime support completely
  - replace use of "type(a) is b" with isinstance(a, b) where appropriate [py3]
  - move loggers out of object instances
  - alter default_file to remove path traversal vulnerability, now discards any path components in file before searching CWD, DEFAULT_PATH and DB_PATH in that order.
  - change module init to default without gtk and don't chdir
  - fix savefile for cross-filesys error, but complain about it
  - re-write system default json
  - add shared config path sanitiser, update apps
  - remove 'print' functions from all modules
  - alter all apps to run 'in-place' from CWD, no more file offsets
  - alter config lock to acquire file handle and return to app
  - use named loggers throughout, attach handlers to root logger
  - convert all use of configparser to jsonconfig, and .ini to .json
  - remove dbexport and geopoint modules completely
  - create new dummy decoder interface, use it to derive other types of decoders
  - move decoder 'type' logic into decoder module, so main app doesn't need to know about subclasses
  - backport fixes from metarace-2.0 and remove DAK support
  - rename rsync module to export, remove FTP support and remove HTTP notify

## roadmeet

  - alter trigger paths to use _timercb and timercb
  - remove scratch pad entirely from ui
  - remove hook for redbull billy cart race
  - update telegraph from 'scb' to 'announce'
  - remove configpath
  - remove hour24, crosslap and sportif (to be converted to stand-alone utils)
  - alter properties dialog
  - remove old menu options for load event and timer configure
  - remove default buttons from roadmeet properties dialog
  - alter remote timing to use ';' as separator as per rrs
  - alter set_timer to allow forced reconnect
  - change key trig to only spawn rfid events 0=trig, else rfid=key
  - remove rfid devices and use decoder library as main timer
  - edit menus and display options
  - remove labels with 'race', change where relevant to 'event'
  - clean up tooltips
  - ditch unused, unimplemented and confusing menu items
  - alter stat but to use a pixbuf for bg, packed using a helper ob
  - fix properties to re-load on change of result categories
  - when exporting startlist and event has cats, export them all

## rms

  - remove 'ready' timerstat
  - remove prepass/preloop completely
  - omit spare cat from result cat lookup
  - convert minlap to mandatory load/save
  - update spare bike substitution
  - fix place assignment with dead heats
  - update logging
  - repair broken cat on lap logic and convert to announcer
  - fix result subheadings for cat and handicap results
  - alter judge rep graphic display to use start instead of catstart
  - fix unicode strings and decodes
  - alter announce outputs for mqtt telegraph
  - fix lap arming for auto lap events
  - fix event reset / clear rider data except for passings
  - change target lap mode to enforce targets and fall back on totlaps
  - disable lapentry and armlap when cat targets are set
  - fix multi cat handling for all reports
  - fix lap count / finish in rf cb
  - repair inconsistencies in sign-on, startlist and judgesrep
  - truncate lap display at finish time, allow inrace rider laps after finish
  - repair startlist export function when cats enabled
  - report uncategorised riders in all reports

## trackmeet

  - add testpage option back into scb menu
  - add 'no time recorded' for ittt handler 'ntr' result
  - repair minor damage in uci hour handler - prepare for simple UI
  - repair damaged prev and next links on trackmeet event reports
  - remove hotlap hack file
  - remove old-style "olympic" omnium handler
  - remove backup timer
  - alter for new-style tod
  - collect timer callback in trackmeet then idle_add curevent cb
  - remove rftimer completely
  - remove dangerous option to deliver DHI over telegraph which
          destroys the announcer and graphicscb output
  - remove rider list and team abbreviations from print program
  - complete utf-8/unicode fixes for strings out of gtk
  - fix path errors loading template files for report
  - remove method default_template()
  - fix local use of 'report' variable
  - improve the main function: bail early if config path not writeable
  - remove load dialog, defer to apploader
  - fix jfile and dirty file notify tool
  - remove printimp
  - rename option uscbport annport
  - remove 'logos' support and overlay control
  - change announcer port to announcer topic, push connection stuff down
  - alter calls into announce to match new pub/sub model
  - leave dangling gfx announce stuff for now
  - remove dbexport
  - move 'announcer' methods back into trackmeet obj and use telegraph
          purely as message exchange
  - remove flap handler entirely and merge functions into f200

## tod

  - remove cdecimal import [py3]
  - complete agg integration and cover all use cases
  - resolve tod/agg ambiguity for arith and negative values
  - fix display formatting errors for tod.agg
  - cleanup tod module interface and rename str2xxx mkxxx
  - remove dangerous copy() method from tod
  - remove dr2t method - outside scope
  - replace DSTHACK with call to datetime in now()
  - replace assert with ValueError for out of range tod
  - update tod input RE to handle all possible tod&agg strings
  - handle float tod value inputs rounded to precision
  - fill in reasonable numeric type handlers for supported arith
  - fix add/sub methods on tod so they don't subclass
  - repair meridian to handle all values of timeval
  - avoid creating copy of Decimal value in constructors
  - ensure truncation is down even for negative values
  - replace bare exception clauses with Exception type
  - add module level logger and report invalid timeval
  - add explicit return values for todlist functions
  - update todlist to include primary and secondary comparison
  - test arithmetic on all types
  - update callers to use tod.now() instead of str2tod('now')
  - update callers to use mktod/mkagg

## rrs

  - re-write as subclass of decoder module
  - update protocol for v2.0
  - change method to polled-passing
  - re-structure I/O to wait for responses before issuing cmd
  - clean up logging to match other decoders
  - fix connect, status and boxname methods
  - remove trig conversion
  - repair trig passthrough and marker detection
  - add battery level reporting
  - alter source and channel to report boxname and loopid resp.

## timy

  - remove glib dependency
  - move logger to module level
  - change strs to unicode
  - alter log lines for cleaner output
  - alter default printing
  - remove unused sectormap
  - use int() to read checksum instead of ord()<<4 | ...
  - fix chandelay so positive delay makes sense
  - fix logging and exception handling
  - remove errstr element
  - remove discontinuity experiment
  - alter default timer log output
  - drop unitno parameter
  - change inner loop delay handling to reduce tod/decimal constructions
  - remove deprecated functions start/stop/sync

## telegraph

  - remove IRC backend from telegraph server completely
  - convert all uses of 'channel' to 'topic' where possible/reasonable
  - merge sender changes to bring both into line
  - alter logging and use of unicode/str
  - fix exceptions
  - don't idle_add receive callback
  - alter send_ ... chan to publish_ ... topic
  - remove json pack from telegraph (use separate lib)
  - with move to pub/sub, remove default channel subscription
  - alter port and topic behaviour for pub/sub
  - replace 'port' command with hard reconnect method
  - substitute paho mqtt library as backend
  - remove all legacy text output and DHI methods
  - remove auto unt4 packages

## report

  - printing library renamed report and printrep renamed report
  - update event list section for self-contained meet index
  - all configured assets loaded through metarace.default_file()
  - exports of "html in text" aka "blog text" are no longer
    supported for any modules. HTML export provides a templating
    system which can be overridden to achieve the original behaviour
    if required.
  - HTML outputs are changed to work with bootstrap 5.0 and to
    support a more natural mobile experience.
  - Add crude three column team startlist
  - add extra digit to secid random number stuff
  - change if blah: to if blah is not None: where blah can be 0
  - clean out hack modes and commented one-offs
  - clean up judgerep laplines
  - add category label to pdf report
  - add lap time export to XLS
  - remove report.output_text() completely
  - remove jump trigger from report html output

## htlib

  - remove shim and js links from htlib emptypage
  - update to bs5.0
  - reorder attrlist to remove '+' on dynamic content
  - add __REPORT_NAV__ macro to emptypage

## scbwin

  - alter result table to send flush packet with single pages to
    keep display active on caprica displays
  - remove flush except when required for page display extension
  - alter testpage to show current scb extents, add method back into tm
  - remove logoanim -> for now
  - remove intro anim

## sender

  - remove dak protocol from sender
  - clean and correct linelen/pagelen in sender
  - use module-level logger in sender lib
  - update log lines
  - remove 'chan' announcer hack from all methods

## race

  - strip out old gfx* methods
  - alter announce for pub/sub
  - announce eliminated rider to topic
  - alter elimination so order is preserved and places override
  - auto flip onto standing after elim command
  - add standings header for elim race + provisional header for result
  - fix distance load error when using properties dialog
  - fix keirin draw number on text announcer
  - confirm elimination with UCI rules

## ittt

  - remove support for flying 200/flying lap
  - fix save/load with jsonconfig
  - update comments and traces as per flying 200
  - alter properties for autotime
  - add method to determine split points based on distance
  - alter result and countbacks to match UCI regs (same as in f200)
  - fix precision values to propery compute and display
  - alter manual timing to refer to split data
  - fix timerpane interaction for split-based intermeds
  - disable autotime on manual intervention
  - automatically arm finish if all laps manually recorded

## f200

  - strip out gfx methods
  - add ntr from ittt
  - fix timerpane callbacks to refer to provided arg
  - load/save comments
  - save and load traces as list of strings instead of join/split
  - update to include config for use with flying lap or any length

## classification

  - clean up and alter log calls
  - fix delayed announce error
  - repair broken autospec lookup ordering

## strops

  - simplify fitname, omit von part and use single concat
  - remove all calls to decode - defer to extraction from gtk
  - move countback out of strops into new module object
  - move search_name into namebank module
  - zero-length unicode chars are not properly handled, but the
    fix would break many other modules. Do not fix.
  - remove obsolete UCI penalty strings and lookups

## riderdb

  - alter biblistfromcat to return list
  - alter save/load to run all strings through unicodedata.normalize()
  - fix listcats to skip cat series and spares

