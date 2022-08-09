#!/usr/bin/python2

import logging
from time import sleep
import metarace
from metarace import timy

logging.basicConfig(level=logging.DEBUG)
metarace.init()


def timercb(impulse):
    print(repr(impulse))


t = timy.timy()
t.setport(u'/dev/ttyS0')
t.setcb(timercb)
t.start()
t.sane()
t.delaytime(u'0.1')
try:
    while True:
        sleep(2)
        t.arm(u'C0')
finally:
    t.exit()
    t.join()
