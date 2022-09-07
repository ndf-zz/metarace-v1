# SPDX-License-Identifier: MIT
"""Telegraph

 Telegraph is a thin wrapper over paho.mqtt.client, which provides
 a MQTT pub/sub interface, altered for use with metarace applications.

 Example:

        import metarace
        from metarace import telegraph
        metarace.init()

        def messagecb(topic, message):
            obj = telegraph.from_json(message)
            ...
        t = telegraph.telegraph()
        t.set_will_json({u'example':[]}, u'thetopic')
        t.subscribe(u'thetopic')
        t.setcb(messagecb)
        t.start()
        ...
        t.publish_json({u'example':[1,2,3]}, u'thetopic')

 Message callback functions receive two named parameters 'topic' and
 'message' which are both unicode strings. The message callback is run in
 the telegraph thread context. Use the convenience function "from_json"
 to convert a message from json into a python object. See defcallback
 for an example.

 Configuration is via metarace system config (metarace.json), under
 section 'telegraph':

  key: (type) Description [default]
  --
  host : (string) MQTT broker, None to disable ['localhost']
  port: (int) MQTT port [1883/8883]
  usetls : (bool) if True, connect to server over TLS [False]
  debug : (bool) if True, enable logging in MQTT library [False]
  username : (string) username [None]
  password : (string) password [None]
  deftopic : (string) a default publish topic [None]
  persist : (bool) if true, open a persistent connection to broker [False]
  clientid : (string) provide an explicit client id [None]
  qos : (int) default QOS to use for subscribe and publish [0]


"""

import threading
import Queue
import logging
import json
import paho.mqtt.client as mqtt
from uuid import uuid4
import metarace

QUEUE_TIMEOUT = 2

# module logger
_log = logging.getLogger(u'metarace.telegraph')
_log.setLevel(logging.DEBUG)


def from_json(payload=None):
    """Return message payload decoded from json, or None."""
    ret = None
    try:
        ret = json.loads(payload)
    except Exception as e:
        _log.warning(u'%s decoding JSON payload: %s', e.__class__.__name__, e)
    return ret


def defcallback(topic=None, message=None):
    """Default message receive callback function."""
    ob = from_json(message)
    if ob is not None:
        _log.debug(u'RCV %r: %r', topic, ob)
    else:
        _log.debug(u'RCV %r: %r', topic, message)


class telegraph(threading.Thread):
    """Metarace telegraph server thread."""

    def subscribe(self, topic=None, qos=None):
        """Add topic to the set of subscriptions."""
        if topic:
            self.__subscriptions[topic] = qos
            if self.__connected:
                self.__queue.put_nowait((u'SUBSCRIBE', topic, qos))

    def unsubscribe(self, topic=None):
        """Remove topic from the set of subscriptions."""
        if topic and topic in self.__subscriptions:
            del self.__subscriptions[topic]
            if self.__connected:
                self.__queue.put_nowait(('UNSUBSCRIBE', topic))

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
        _log.debug(u'Default publish topic set to: %r', self.__deftopic)

    def set_will_json(self, obj=None, topic=None, qos=None, retain=False):
        """Pack the provided object into JSON and set as will."""
        try:
            self.set_will(json.dumps(obj), topic, qos, retain)
        except Exception as e:
            _log.error(u'Error setting will object %r: %s', obj, e)

    def set_will(self, message=None, topic=None, qos=None, retain=False):
        """Set or clear the last will with the broker."""
        if not self.__connect_pending and not self.__connected:
            if topic is not None:
                nqos = qos
                if nqos is None:
                    nqos = self.__qos
                payload = None
                if message is not None:
                    payload = message.encode('utf-8')
                self.__client.will_set(topic, payload, nqos, retain)
                _log.debug(u'Will set on topic %r', topic)
            else:
                self.__client.will_clear()
                _log.debug(u'Cleared will')
        else:
            _log.error(u'Unable to set will, already connected')

    def connected(self):
        """Return true if connected."""
        return self.__connected

    def reconnect(self):
        """Request re-connection to broker."""
        self.__queue.put_nowait((u'RECONNECT', True))

    def exit(self, msg=None):
        """Request thread termination."""
        self.__running = False
        self.__queue.put_nowait((u'EXIT', msg))

    def wait(self):
        """Suspend calling thread until command queue is processed."""
        self.__queue.join()

    def publish(self, message=None, topic=None, qos=None, retain=False):
        """Publish the provided msg to topic or deftopic if None."""
        self.__queue.put_nowait((u'PUBLISH', topic, message, qos, retain))

    def publish_json(self, obj=None, topic=None, qos=None, retain=False):
        """Pack the provided object into JSON and publish to topic."""
        try:
            self.publish(unicode(json.dumps(obj)), topic, qos, retain)
        except Exception as e:
            _log.error(u'Error publishing object %r: %s', obj, e)

    def __init__(self):
        """Constructor."""
        threading.Thread.__init__(self)
        self.daemon = True
        self.__queue = Queue.Queue()
        self.__cb = defcallback
        self.__subscriptions = {}
        self.__deftopic = None
        self.__connected = False
        self.__connect_pending = False
        self.__host = 'localhost'
        self.__port = 1883
        self.__cid = None
        self.__persist = False
        self.__resub = True
        self.__qos = 0
        self.__doreconnect = False

        # check system config for overrides
        if metarace.sysconf.has_option(u'telegraph', u'host'):
            self.__host = metarace.sysconf.get_str(u'telegraph', u'host')
        if metarace.sysconf.has_option(u'telegraph', u'deftopic'):
            # note: this may be overidden by application
            self.__deftopic = metarace.sysconf.get_str(u'telegraph',
                                                       u'deftopic')
        if metarace.sysconf.has_option(u'telegraph', u'qos'):
            self.__qos = metarace.sysconf.get_posint(u'telegraph', u'qos', 0)
            if self.__qos > 2:
                _log.info(u'Invalid QOS %r set to %r', self.__qos, 2)
                self.__qos = 2
        if metarace.sysconf.has_option(u'telegraph', u'clientid'):
            self.__cid = metarace.sysconf.get_str(u'telegraph', u'clientid')
        if not self.__cid:
            self.__cid = str(uuid4())
        _log.debug(u'Using client id: %r', self.__cid)
        if metarace.sysconf.has_option(u'telegraph', u'persist'):
            self.__persist = metarace.sysconf.get_bool(u'telegraph',
                                                       u'persist')
        _log.debug(u'Persistent connection: %r', self.__persist)

        # create mqtt client
        self.__client = mqtt.Client(client_id=self.__cid,
                                    clean_session=not self.__persist)
        if metarace.sysconf.has_option(u'telegraph', u'debug'):
            if metarace.sysconf.get_bool(u'telegraph', u'debug'):
                _log.debug(u'Enabling mqtt/paho debug')
                mqlog = logging.getLogger(u'metarace.telegraph.mqtt')
                mqlog.setLevel(logging.DEBUG)
                self.__client.enable_logger(mqlog)
        if metarace.sysconf.has_option(u'telegraph', u'usetls'):
            if metarace.sysconf.get_bool('telegraph', 'usetls'):
                _log.debug(u'Enabling TLS connection')
                self.__port = 8883
                self.__client.tls_set()
        username = None
        password = None
        if metarace.sysconf.has_option(u'telegraph', u'username'):
            username = metarace.sysconf.get_str(u'telegraph', u'username')
        if metarace.sysconf.has_option(u'telegraph', u'password'):
            password = metarace.sysconf.get_str(u'telegraph', u'password')
        if username and password:
            self.__client.username_pw_set(username, password)
        # override automatic port selection if provided
        if metarace.sysconf.has_option(u'telegraph', u'port'):
            np = metarace.sysconf.get_posint(u'telegraph', u'port')
            if np is not None:
                self.__port = np
                _log.debug(u'Set port to %r', self.__port)
        self.__client.reconnect_delay_set(2, 16)
        self.__client.on_message = self.__on_message
        self.__client.on_connect = self.__on_connect
        self.__client.on_disconnect = self.__on_disconnect
        if self.__host:
            self.__doreconnect = True
        self.__running = False

    def __reconnect(self):
        if not self.__connect_pending:
            if self.__connected:
                _log.debug(u'Disconnecting client')
                self.__client.disconnect()
                self.__client.loop_stop()
            if self.__host:
                _log.debug(u'Connecting to %s:%d', self.__host, self.__port)
                self.__connect_pending = True
                self.__client.connect_async(self.__host, self.__port)
                self.__client.loop_start()

    # PAHO methods
    def __on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            _log.debug(u'Connect %r: %r/%r', client._client_id, flags, rc)
            if not self.__resub and self.__persist and flags[
                    u'session present']:
                _log.debug(u'Resumed existing session for %r',
                           client._client_id)
            else:
                _log.debug(u'Assuming Clean session for %r', client._client_id)
                s = []
                for t in self.__subscriptions:
                    nqos = self.__subscriptions[t]
                    if nqos is None:
                        nqos = self.__qos
                    s.append((t, nqos))
                if len(s) > 0:
                    _log.debug(u'Subscribe topics: %r', s)
                    self.__client.subscribe(s)
                self.__resub = False
            self.__connected = True
        else:
            _log.info(u'Connect failed with error %r: %r', rc,
                      mqtt.connack_string(rc))
            self.__connected = False
        self.__connect_pending = False

    def __on_disconnect(self, client, userdata, rc):
        _log.debug(u'Disconnect %r: %r', client._client_id, rc)
        self.__connected = False
        # Note: PAHO lib will attempt re-connection automatically

    def __on_message(self, client, userdata, message):
        #_log.debug(u'Message from %r: %r', client._client_id, message)
        self.__cb(topic=message.topic,
                  message=message.payload.decode(u'utf-8'))

    def run(self):
        """Called via threading.Thread.start()."""
        self.__running = True
        if self.__host:
            _log.debug(u'Starting')
        else:
            _log.debug(u'Not connected')
        while self.__running:
            try:
                # Check connection status
                if self.__host and self.__doreconnect:
                    self.__doreconnect = False
                    if not self.__connect_pending:
                        self.__reconnect()
                # Process command queue
                while self.__running:
                    m = self.__queue.get(timeout=QUEUE_TIMEOUT)
                    self.__queue.task_done()
                    if m[0] == u'PUBLISH':
                        ntopic = self.__deftopic
                        if m[1] is not None:  # topic is set
                            ntopic = m[1]
                        nqos = m[3]
                        if nqos is None:
                            nqos = self.__qos
                        if ntopic:
                            msg = None
                            if m[2] is not None:
                                msg = m[2].encode(u'utf-8')
                            self.__client.publish(ntopic, msg, nqos, m[4])
                        else:
                            #_log.debug(u'No topic, msg ignored: %r', m[1])
                            pass
                    elif m[0] == u'SUBSCRIBE':
                        _log.debug(u'Subscribe topic: %r', m[1])
                        nqos = m[2]
                        if nqos is None:
                            nqos = self.__qos
                        self.__client.subscribe(m[1], nqos)
                    elif m[0] == u'UNSUBSCRIBE':
                        _log.debug(u'Un-subscribe topic: %r', m[1])
                        self.__client.unsubscribe(m[1])
                    elif m[0] == u'RECONNECT':
                        self.__connect_pending = False
                        self.__doreconnect = True
                    elif m[0] == u'EXIT':
                        _log.debug(u'Request to close: %r', m[1])
                        self.__running = False
            except Queue.Empty:
                pass
            except Exception as e:
                _log.error(u'%s: %s', e.__class__.__name__, e)
                self.__connect_pending = False
                self.__doreconnect = False
        self.__client.disconnect()
        self.__client.loop_stop()
        _log.info(u'Exiting')
