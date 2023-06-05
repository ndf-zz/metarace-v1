# metarace-v1

Deprecated toolkit for Python 2 with static Gtk bindings.

**Do not use**

See related packages:

   - [metarace-roadmeet](https://github.com/ndf-zz/metarace-roadmeet): Road race timing & results
   - [metarace-tagreg](https://github.com/ndf-zz/metarace): Manage transponder ids
   - [metarace](https://github.com/ndf-zz/metarace): Base library


## Changes

In preparation for version 2, this library makes the following major
departures from older releases:

   - System defaults moved to ~/Documents/metarace/default
   - "Meet" is now the toplevel type for both track and road races
   - Riderdb columns have been altered to better align with version 2
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
