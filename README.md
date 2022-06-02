# metarace-v1

Metarace cycle race tools for Python2/pygtk.

This is a transitional update of the metarace library
toward Python3 and gtk/gi. In most cases, a new installation
will not be able to meet the requirements of the static 
gtk bindings this relies on, so don't install without testing
a source distribution first.


## Pre-Requisites

Static gtk, cairo, pango and rsvg libraries:

   - pygtk
   - gtk
   - glib
   - gobject
   - cairo
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

   - Telegraph is now a thin wrapper on MQTT, completely replacing
     the older IRC-backed library with a simpler publish/subscribe
     approach.
   - Single-event, obscure hardware and special-purpose support scripts
     have been removed from the library. The only remaining top level
     scripts are: apploader, trackmeet and roadmeet.
   - Transponder decoder interfaces have been altered to be instances
     of a new decoder library, greatly simplifying support for different
     hardware.
   - Several libraries have been renamed and adjusted to match common
     use (eg printing/report and mirror/export).
   - Application features have been reduced and standardised to meet
     common use.

For a list of changes from the last published pygtk version (1.11) 
please refer to [v1-changes.md](v1-changes.md).


## Testing

	$ cd src
	$ export PYTHONPATH=.
	$ python2 -m metarace.roadmeet
