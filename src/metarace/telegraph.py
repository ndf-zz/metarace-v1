"""Telegraph - MQTT backed message exchange service.

 Configuration via metarace system config metarace.json, keys:

  host : (string) hostname or IP of MQTT server, or None to disable
  usetls : (bool) if True, connect to server over TLS
  debug : (bool) if True, enable logging in MQTT library
  username : (string) username or None to disable
  password : (string) password or None to disable
  deftopic : (string) a default publish topic or None to disable
  qos : (int) QOS to use for topic subscriptions

"""

import threading
import Queue
import logging
import json
import paho.mqtt.client as mqtt
import metarace
from metarace import strops

QUEUE_TIMEOUT = 2

# module logger
LOG = logging.getLogger(u'metarace.telegraph')
LOG.setLevel(logging.DEBUG)


def defcallback(topic=None, msg=None):
    """Default message receive callback function."""
    LOG.debug(u'RCV %r: %r', topic, msg)


class telegraph(threading.Thread):
    """Metarace telegraph server thread."""

    def subscribe(self, topic=None):
        """Add topic to the set of subscriptions."""
        if topic:
            self.__subscriptions.add(topic)
            if self.__connected:
                self.__queue.put_nowait((u'SUBSCRIBE', topic))

    def unsubscribe(self, topic=None):
        """Remove topic from the set of subscriptions."""
        if topic and topic in self.__subscriptions:
            self.__subscriptions.remove(topic)
            if self.__connected:
                self.__queue.put_nowait((u'UNSUBSCRIBE', topic))

    def setcb(self, func=None):
        """Set the message receive callback function."""
        if func is not None:
            self.__cb = func
        else:
            self.__cb = defcallback

    def set_deftopic(self, topic=None):
        """Set or clear the default publish topic."""
        if isinstance(topic, basestring) and topic:
            self.__deftopic = topic
        else:
            self.__deftopic = None
        LOG.debug(u'Default publish topic set to: %r', self.__deftopic)

    def set_clientid(self, clientid=None):
        """Set or clear the MQTT Client ID."""
        if isinstance(clientid, basestring) and clientid:
            self.__client._client_id = clientid
        else:
            self.__client.__client_id = u''
        LOG.debug(u'MQTT client ID set to: %r', self.__client._client_id)
        if self.__connected:
            self.reconnect()

    def connected(self):
        """Return true if connected."""
        return self.__connected

    def reconnect(self):
        """Request re-connection to relay."""
        self.__queue.put_nowait((u'RECONNECT', True))

    def exit(self, msg=None):
        """Request thread termination."""
        self.__running = False
        self.__queue.put_nowait((u'EXIT', msg))

    def wait(self):
        """Suspend calling thread until command queue is processed."""
        self.__queue.join()

    def publish(self, msg=None, topic=None, qos=0, retain=False):
        """Publish the provided msg to topic or deftopic if None."""
        self.__queue.put_nowait((u'PUBLISH', topic, msg, qos, retain))

    def publish_json(self, obj=None, topic=None, qos=0, retain=False):
        """Pack the provided object into JSON and publish to topic."""
        try:
            self.publish(unicode(json.dumps(obj)), topic)
        except Exception as e:
            LOG.error(u'Error publishing object %r: %s', obj, e)

    # Legacy deprecated publish methods
    def publish_unt4(self, unt4msg, topic=None):
        """Pack and publish a unt4 message to topic."""
        LOG.error(u'Deprecated publish_unt4()')
        #self.publish(unt4msg.pack(), topic)

    def clrall(self, topic=None):
        """Publish general clearing to the specified topic."""
        LOG.error(u'Deprecated method clrall()')
        #self.publish_unt4(unt4.GENERAL_CLEARING, topic)

    def clrline(self, line, topic=None):
        """Publish DHI clear line to topic."""
        LOG.error(u'Deprecated method clrline(%r)', line)
        #self.publish_unt4(unt4.unt4(xx=0,yy=int(line),erl=True), topic)

    def set_title(self, line, topic=None):
        """Publish announcer title line."""
        LOG.error(u'Deprecated method set_title(%r)', line)
        #self.publish_unt4(unt4.unt4(header=u'title',text=line), topic)

    def set_time(self, tstr, topic=None):
        """Publish announcer time."""
        LOG.error(u'Deprecated method set_time(%r)', tstr)
        #self.publish_unt4(unt4.unt4(header=u'time',text=tstr), topic)

    def set_start(self, stod, topic=None):
        """Publish announcer relative start time."""
        LOG.error(u'Deprecated method set_start(%r)', stod)
        #self.publish_unt4(unt4.unt4(header=u'start',text=stod.rawtime()), topic)

    def set_gap(self, tstr, topic=None):
        """Publish announcer gap time (if relevant)."""
        LOG.error(u'Deprecated method set_gap(%r)', tstr)
        #self.publish_unt4(unt4.unt4(header=u'gap',text=tstr), topic)

    def set_avg(self, tstr, topic=None):
        """Publish announcer average speed."""
        LOG.error(u'Deprecated method set_avg(%r)', tstr)
        #self.publish_unt4(unt4.unt4(header=u'average',text=tstr), topic)

    def add_rider(self, rvec, header_txt=u'rider', topic=None):
        """Publish rider vector."""
        LOG.error(u'Deprecated method add_rider(%r,%r)', rvec, header_txt)
        #self.publish_unt4(unt4.unt4(header=header_txt,
        #text=unichr(unt4.US).join(rvec)), topic)

    def gfx_overlay(self, newov=u'', topic=None):
        """Publish update graphic overlay."""
        LOG.error(u'Deprecated method gfx_overlay(%r)', newov)
        #self.publish_unt4(unt4.unt4(header=u'overlay',
        #text=unicode(newov)), topic)

    def gfx_set_title(self, title=u'', topic=None):
        """Publish graphic channel title."""
        LOG.error(u'Deprecated method gfx_set_title(%r)', title)
        #self.publish_unt4(unt4.unt4(header=u'set_title', text=title), topic)

    def gfx_add_row(self, rvec, topic=None):
        """Publish result row to graphic overlay."""
        LOG.error(u'Deprecated method gfx_add_row(%r)', rvec)
        #ovec = []
        #for c in rvec:	# replace nulls and empties
        #nc = u''
        #if c:	# assume string content
        #nc = c
        #ovec.append(nc)
        #self.publish_unt4(unt4.unt4(header=u'add_row',
        #text=unichr(unt4.US).join(ovec)), topic)

    def setline(self, line, msg, topic=None):
        """Publish a DHI set line to topic."""
        LOG.error(u'Deprecated method setline(%r, %r)', line, msg)
        #msg = msg[0:self.linelen].ljust(self.linelen)
        #self.publish_unt4(unt4.unt4(xx=0,yy=int(line),text=msg),topic)

    def flush(self, topic=None):
        """Publish a flush packet to topic."""
        LOG.error(u'Deprecated method flush()')
        #self.publish_unt4(unt4.GENERAL_EMPTY, topic)

    def linefill(self, line, char=u'_', topic=None):
        """Publish a linefill to topic using supplied char."""
        LOG.error(u'Deprecated method linefill(%r, %r)', line, char)
        #msg = char * self.linelen
        #self.publish_unt4(unt4.unt4(xx=0,yy=int(line),text=msg), topic)

    def postxt(self, line, oft, msg, topic=None):
        """Publish a DHI positioned text message to topic."""
        LOG.error(u'Deprecated method postxt(%r, %r, %r)', line, oft, msg)
        #if oft >= 0:
        #self.publish_unt4(unt4.unt4(xx=int(oft),yy=int(line),text=msg),
        #topic)

    def setoverlay(self, newov, topic=None):
        """Publish overlay update if required to topic."""
        LOG.error(u'Deprecated method setoverlay(%r)', newov)
        #if self.__curov != newov:
        #self.publish_unt4(newov, topic)
        #self.__curov = newov

    def __init__(self):
        """Constructor."""
        threading.Thread.__init__(self)
        self.__queue = Queue.Queue()
        self.__cb = defcallback
        self.__subscriptions = set()
        self.__curov = None
        self.__deftopic = None
        self.__connected = False
        self.__connect_pending = False
        self.__host = None
        self.__qos = 0
        self.__doreconnect = True  # if host set, try to connect on startup

        # check system config for overrides
        if metarace.sysconf.has_option(u'telegraph', u'host'):
            self.__host = metarace.sysconf.get(u'telegraph', u'host')
        if metarace.sysconf.has_option(u'telegraph', u'deftopic'):
            # note: this may be overidden by application
            self.__deftopic = metarace.sysconf.get(u'telegraph', u'deftopic')
        if metarace.sysconf.has_option(u'telegraph', u'qos'):
            self.__qos = strops.confopt_posint(
                metarace.sysconf.get(u'telegraph', u'qos'), 0)
            if self.__qos > 2:
                LOG.info(u'Invalid QOS %r set to %r', self.__qos, 2)
                self.__qos = 2

        # create mqtt client
        self.__client = mqtt.Client()
        if metarace.sysconf.has_option(u'telegraph', u'debug'):
            if strops.confopt_bool(metarace.sysconf.get(
                    u'telegraph', u'debug')):
                LOG.debug(u'Enabling mqtt/paho debug')
                mqlog = logging.getLogger(u'metarace.telegraph.mqtt')
                mqlog.setLevel(logging.DEBUG)
                self.__client.enable_logger(mqlog)
        if metarace.sysconf.has_option(u'telegraph', u'usetls'):
            if strops.confopt_bool(
                    metarace.sysconf.get(u'telegraph', u'usetls')):
                LOG.debug('Enabling TLS connection')
                self.__client.tls_set()
        username = None
        password = None
        if metarace.sysconf.has_option(u'telegraph', u'username'):
            username = metarace.sysconf.get(u'telegraph', u'username')
        if metarace.sysconf.has_option(u'telegraph', u'password'):
            password = metarace.sysconf.get(u'telegraph', u'password')
        if username and password:
            self.__client.username_pw_set(username, password)
        self.__client.reconnect_delay_set(2, 16)
        self.__client.on_message = self.__on_message
        self.__client.on_connect = self.__on_connect
        self.__client.on_disconnect = self.__on_disconnect
        self.__running = False

    def __reconnect(self):
        if not self.__connect_pending:
            if self.__connected:
                LOG.debug(u'Disconnecting client')
                self.__client.disconnect()
            if self.__host:
                LOG.debug(u'Connecting to %s', self.__host)
                self.__connect_pending = True
                self.__client.connect_async(self.__host)

    # PAHO methods
    def __on_connect(self, client, userdata, flags, rc):
        LOG.debug(u'Connect %r: %r/%r', client._client_id, flags, rc)
        s = [(t, self.__qos) for t in self.__subscriptions]
        if len(s) > 0:
            LOG.debug(u'Subscribe: %r', s)
            self.__client.subscribe(s)
        self.__connect_pending = False
        self.__connected = True

    def __on_disconnect(self, client, userdata, rc):
        LOG.debug(u'Disconnect %r: %r', client._client_id, rc)
        self.__connected = False
        # Note: PAHO lib will attempt re-connection automatically

    def __on_message(self, client, userdata, message):
        #LOG.debug(u'Message from %r: %r', client._client_id, message)
        self.__cb(message.topic, message.payload.decode(u'utf-8'))

    def run(self):
        """Called via threading.Thread.start()."""
        self.__running = True
        if self.__host:
            LOG.debug(u'Starting')
        else:
            LOG.debug(u'Not connected')
        self.__client.loop_start()
        while self.__running:
            try:
                # Check connection status
                if self.__host and self.__doreconnect:
                    self.__doreconnect = False
                    if not self.__connect_pending:
                        self.__reconnect()
                # Process command queue
                while True:
                    m = self.__queue.get(timeout=QUEUE_TIMEOUT)
                    self.__queue.task_done()
                    if m[0] == u'PUBLISH':
                        ntopic = self.__deftopic
                        if m[1] is not None:  # topic is set
                            ntopic = m[1]
                        if ntopic:
                            msg = None
                            if m[2] is not None:
                                msg = m[2].encode(u'utf-8')
                            self.__client.publish(ntopic, msg, m[3], m[4])
                        else:
                            #LOG.debug(u'No topic, msg ignored: %r', m[1])
                            pass
                    elif m[0] == u'SUBSCRIBE':
                        LOG.debug(u'Subscribe topic: %r', m[1])
                        self.__client.subscribe(m[1], self.__qos)
                    elif m[0] == u'UNSUBSCRIBE':
                        LOG.debug(u'Un-subscribe topic: %r', m[1])
                        self.__client.unsubscribe(m[1])
                    elif m[0] == u'RECONNECT':
                        self.__connect_pending = False
                        self.__doreconnect = True
                    elif m[0] == u'EXIT':
                        LOG.debug(u'Request to close: %r', m[1])
                        self.__running = False
            except Queue.Empty:
                pass
            except Exception as e:
                LOG.error(u'%s: %s', e.__class__.__name__, e)
                self.__connect_pending = False
                self.__doreconnect = False
        self.__client.disconnect()
        self.__client.loop_stop()
        LOG.info(u'Exiting')
