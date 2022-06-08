# metarace-v1

Cyclesport results and timing toolkit for Python2/pygtk.

This is a transitional, and in some cases, incompatible update
of the metarace package toward Python3 and gtk/gi. Before updating
from version 1.11 to 1.12, please read [Changes](#Changes) below
and check the example systems default file
[metarace.json](src/metarace/data/metarace.json) for any required updates.

Version 1.12 will be the final release of metarace version 1.
See related package [metarace](https://github.com/ndf-zz/metarace)
(version 2) for on-going python3/gi support.


## Pre-Requisites

Static gtk, cairo, pango and rsvg libraries:

   - python2
   - pygtk
   - gtk
   - glib
   - gobject
   - pycairo
   - pango
   - pangocairo
   - rsvg

Support libraries:

   - paho-mqtt
   - pyserial
   - xlwt
   - libscrc


## Changes

In preparation for version 2, this library makes the following major
departures from older releases:

   - System defaults moved to ~/Documents/metarace/default
   - "Meet" is now the toplevel type for both track and road races.
   - Telegraph is a thin wrapper on MQTT, completely replacing
     the IRC-backed library with a simpler publish/subscribe
     approach.
   - Single-event, obscure hardware and special-purpose support scripts
     have been removed from the package. The only remaining console
     scripts are: metarace, trackmeet and roadmeet.
   - Transponder decoder interfaces have been altered to be instances
     of a new decoder library. Currently supported decoders:
     Race Result System, Race Result USB, and Chronelec (Tag Heuer)
     Protime/Elite RC/LS.
   - Several modules have been renamed and adjusted to match common
     use (eg printing/report and mirror/export).
   - Application features have been reduced and standardised to meet
     common use.
   - Configparser style .ini files are removed and replaced
     with JSON-backed jsonconfigs throughout.
   - All configuration and setting files are written to disk via
     metarace.savefile(). Related meets which share resources through
     symbolic links should be aware that savefile() writes to real file
     contained in the meet folder, ignoring symbolic links out of the
     meet if they exist.
   - Meet and event configuration options have been adjusted, and in many
     cases, older configs (v1.11 and earlier) may not be correctly read.
   - Default file handling no longer allows fetching of resource files
     from relative paths. Files are expected to be available in the
     meet folder, or the system defaults folder
     (~/Documents/metarace/default)

For a list of changes from the last published pygtk version (1.11) 
please refer to [v1-changes.md](v1-changes.md).


## Installation

	# pip2 install metarace<2


## Applications

Version 1 of the metarace package includes the following
top-level applications: roadmeet, trackmeet and metarace.
Version 2+ releases of the package will not include applications,
these will be distributed separately. See related project
[metarace](https://github.com/ndf-zz/metarace) for more information.


### Roadmeet

Timing and result application for UCI Part 2 Road Races,
UCI Part 5 Cyclo-Cross, criterium, road handicap and ad-hoc
time trial events.

	usage: roadmeet [meetpath]


### Trackmeet

Timing and result application for track meets with support for
UCI Part 3 Track Races as well as handicap (wheelrace) and
hour record events.

	usage: trackmeet [meetpath]


### Apploader (metarace)

Convenience application to browse, create and run metarace meets.

	usage: metarace [meetpath]

