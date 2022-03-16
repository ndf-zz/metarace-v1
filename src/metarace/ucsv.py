"""Transitional Python 2.x unicode csv helper module."""

# Copied from the Python manual:
#
#  https://docs.python.org/release/2.7.16/library/csv.html#csv-examples
#
# Note: - Contrary to the typical behaviour of a utf-8 decoder,
#         invalid, erroeneous or non-unicode chars are silently ignored
# 	- Some auto-generated CSV with newlines in cells will not read properly
#	- This module _has_ to go.

import csv, codecs, cStringIO

QUOTE_MINIMAL = csv.QUOTE_MINIMAL


class UTF8Recoder:
    """
    Iterator that reads an encoded stream and reencodes the input to UTF-8
    """

    def __init__(self, f, encoding):
        self.reader = codecs.getreader(encoding)(f, errors='ignore')

    def __iter__(self):
        return self

    def next(self):
        return self.reader.next().encode('utf-8', 'ignore')


class UnicodeReader:
    """
    A CSV reader which will iterate over lines in the CSV file "f",
    which is encoded in the given encoding.
    """

    def __init__(self, f, dialect=None, encoding="utf-8", **kwds):
        if dialect is None:
            try:
                dialect = csv.Sniffer().sniff(f.read(1024))
            except:
                pass
            f.seek(0)
        f = UTF8Recoder(f, encoding)
        self.reader = csv.reader(f, dialect=dialect, **kwds)

    def next(self):
        row = self.reader.next()
        return [unicode(s, "utf-8") for s in row]

    def __iter__(self):
        return self


class UnicodeWriter:
    """
    A CSV writer which will write rows to CSV file "f",
    which is encoded in the given encoding.
    """

    def __init__(self,
                 f,
                 dialect=csv.excel,
                 encoding="utf-8",
                 quoting=csv.QUOTE_NONNUMERIC,
                 **kwds):
        # Redirect output to a queue
        dialect.quoting = quoting
        self.queue = cStringIO.StringIO()
        self.writer = csv.writer(self.queue, dialect=dialect, **kwds)
        self.stream = f
        self.encoder = codecs.getincrementalencoder(encoding)(errors='ignore')

    def writerow(self, row):
        self.writer.writerow([s.encode("utf-8", 'ignore') for s in row])
        # Fetch UTF-8 output from the queue ...
        data = self.queue.getvalue()
        data = data.decode("utf-8", 'ignore')
        # ... and reencode it into the target encoding
        data = self.encoder.encode(data)
        # write to the target stream
        self.stream.write(data)
        # empty queue
        self.queue.truncate(0)

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)
