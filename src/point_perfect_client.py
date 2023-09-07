#!/usr/bin/env python3

"""
    u-blox PointPerfect MQTT client with AssistNow and localization support

    Run with -h (or --help) to see supported command line arguments

    Command line examples:

    Using continental corrections for Europe:
    python pointperfect_client.py -P <port> -j <U-Center JSON config> --region eu

    Using auto-detected regional/continental corrections:
    python pointperfect_client.py -P <port> -j <U-Center JSON config>

    Using localized corrections:
    python pointperfect_client.py -P <port> -j <U-Center JSON config> -l

    Download the U-Center JSON config from the u-blox Thingstream portal:
    "Thingstream > Location Services > Location Thing > credentials"
    Alternatively, use the key/certificate files and client ID from the portal.

    <port> is the serial port of the u-blox GNSS receiver with SPARTN support,
    e.g. /dev/ttyACM0 or COM3. Optionally with baudrate, e.g. /dev/ttyACM0@115200.
"""


import argparse
import json
import logging
import os.path
import re
import sys
import tempfile
import time

from math import radians, floor, cos, pi

# pip install paho-mqtt
import paho.mqtt.client as mqtt

# Center point of rectangular regions mapped to the region name. Used
# for detecting the region based on proximity to one of these points.
# These mappings may be inaccurate or out of date. Please check the
# PointPerfect documentation for the latest information and consider
# using the --region option to manually specify the correct region.
REGION_MAPPING = {
    'S2655E13470': 'au',
    'N5245E01185': 'eu',
    'N3895E13960': 'jp', # East
    'N3310E13220': 'jp', # West
    'N3630E12820': 'kr',
    'N3920W09660': 'us',
}

QUALITIES = ('NOFIX', 'GNSS', 'DGNSS', 'PPS', 'FIXED', 'FLOAT', 'DR', 'MAN', 'SIM')

STATS = 100  # logging level for stats

class NmeaParser:
    '''
    Parse NMEA sentences from bytes and invoke callbacks for matching sentences.
    Strips newlines before passing the sentence to the callback. Errors are silently
    ignored and the parser is robust to malformed sentences or UBX, RTCM, SPARTN, etc.
    '''

    def __init__(self, callbacks):
        '''
        Initialize the parser with a dictionary of callbacks for matching sentences.

        Parameters:
            callbacks (dict): Dictionary of compiled regular expressions objects mapped
                              to callbacks.
        '''
        self.callbacks = callbacks
        self.buffer = None

    def parse(self, data):
        '''Parse the given bytes and invoke callbacks for matching sentences.'''
        for byte in data:
            if byte == ord('$'):
                self.buffer = bytearray([byte])
            elif self.buffer is not None:
                if (byte in range(ord('A'), ord('Z')+1) or
                    byte in range(ord('0'), ord('9')+1) or
                    byte in (ord(','), ord('.'), ord('-'), ord('*'))):
                    self.buffer.append(byte)
                elif byte == 0x0d:  # CR
                    if len(self.buffer) > 3 and self.buffer[-3] == ord('*'):
                        try:
                            chksum_received = int(self.buffer[-2:], 16)
                        except ValueError:
                            chksum_received = -1  # will never match below
                        chksum = 0
                        for i in self.buffer[1:-3]:
                            chksum ^= i
                        if chksum == chksum_received:
                            for regexp in self.callbacks.keys():
                                if regexp.match(self.buffer):
                                    data = self.buffer.decode(encoding='ascii')
                                    # invoke callback with data
                                    self.callbacks[regexp](data)
                        else:
                            logging.warning('chksum error: %02x != %02x',
                                                chksum_received, chksum)
                    self.buffer = None
                else:
                    self.buffer = None


class PointPerfectClient:
    '''
    u-blox PointPerfect MQTT client with AssistNow and localization support

    Subscribes to the PointPerfect MQTT service and sends corrections to
    a u-blox receiver with native SPARTN support. Monitors the receiver's
    position in order to subscribe to the appropriate corrections.
    '''
    EARTH_CIRCUMFERENCE = 6371000 * 2 * pi
    NSEW_TO_SIGN = str.maketrans('NSEW', '0-0-')

    def __init__(self, gnss, mqtt_client, mqtt_server, mqtt_port,
                 localized=False, tile_level=0, lband=False, region=None,
                 distance=50000, epochs=float('inf'), ubxfile=None, stats=None,
                 assist_now = False):
        self.gnss = gnss
        self.mqtt_client = mqtt_client
        self.mqtt_server = mqtt_server
        self.mqtt_port = mqtt_port
        self.localized = localized
        self.distance = distance
        self.epochs = epochs
        self.tile_level = tile_level
        self.ubxfile = ubxfile
        self.plan = 'Lb' if lband else 'ip'
        self.lat = 0  # lat at which node selection was last performed
        self.lon = 0  # lon at which node selection was last performed
        self.epoch_count = 0  # number of epochs since last node selection
        self.dlat_threshold = distance * 360 / self.EARTH_CIRCUMFERENCE
        self.dlon_threshold = 0  # will be set in process_position()
        self.tile_dict = None  # cached tile data
        self.tile_topic = ''  # current tile topic
        self.spartn_topic = ''  # current SPARTN topic
        self.assist_now = assist_now  # True means always use AssistNow
        self.assist_now_topic = '/pp/ubx/mga' if assist_now else None
        self.connected = False
        self.new_server = None  # if set, connect to this server after disconnect

        if stats:
            self.stats = type('stat', (object,), { 'epochs': [0] * len(QUALITIES),
                                                   'total': 0, 'interval': stats })
        else:
            self.stats = None

        handlers = { re.compile(b'^\\$G[A-Z]GGA,'): self.handle_nmea_gga }
        self.nmea_parser = NmeaParser(handlers)

        self.mqtt_topics = []
        if not localized:
            self.mqtt_topics.append((f'/pp/ubx/0236/{self.plan}', 1))
            if region:
                self.spartn_topic = f'/pp/{self.plan}/{region}'

        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        self.mqtt_client.on_message = self.on_mqtt_message

        while True:
            try:
                logging.info('Connecting to %s', self.mqtt_server)
                self.mqtt_client.connect(self.mqtt_server, self.mqtt_port)
                break
            except OSError:
                logging.warning("MQTT connection failed, retrying ...")
                time.sleep(5)
        self.mqtt_client.loop_start()


    def on_mqtt_connect(self, mqtt_client, userdata, flags, return_code):
        '''Callback for handling MQTT connection.'''
        del userdata, flags  # unused
        if return_code == 0:
            self.connected = True
            logging.info('Connected to %s', self.mqtt_server)
            if self.mqtt_topics:
                for topic_qos_tuple in self.mqtt_topics:
                    logging.info('Subscribing to %s', topic_qos_tuple[0])
                mqtt_client.subscribe(self.mqtt_topics)
            if self.spartn_topic:
                logging.info('Subscribing to %s', self.spartn_topic)
                mqtt_client.subscribe((self.spartn_topic, 0))
            if self.assist_now_topic:
                logging.info('Subscribing to %s', self.assist_now_topic)
                qos = 0 if self.assist_now_topic.endswith('/updates') else 1
                mqtt_client.subscribe((self.assist_now_topic, qos))
        else:
            logging.error('Failed to connect, return code %d', return_code)


    def on_mqtt_disconnect(self, mqtt_client, userdata, return_code):
        '''Callback for MQTT disconnect'''
        del mqtt_client, userdata  # unused
        self.connected = False
        if self.new_server:
            self.mqtt_server = self.new_server
            self.new_server = None
            logging.info('Connecting to %s', self.mqtt_server)
            self.mqtt_client.connect(self.mqtt_server, self.mqtt_port)
        if return_code != 0:
            logging.error('Unexpected MQTT disconnect')


    def on_mqtt_message(self, mqtt_client, userdata, msg):
        '''Callback for handling MQTT messages.'''
        del userdata  # unused
        if msg.topic.startswith(f'/pp/{self.plan}/'):
            # received regional SPARTN; send to receiver
            self.gnss.write(msg.payload)
        elif msg.topic.startswith('/pp/ubx/'):
            # received SPARTN key or AssistNow data; send to receiver
            self.gnss.write(msg.payload)
            if msg.topic == '/pp/ubx/mga':
                mqtt_client.unsubscribe('/pp/ubx/mga')
                self.assist_now_topic = '/pp/ubx/mga/updates'
                logging.info('Subscribing to %s', self.assist_now_topic)
                mqtt_client.subscribe((self.assist_now_topic, 0))
        elif msg.topic.startswith('pp/ip'):
            if msg.topic.endswith('/dict'):
                self.process_tile_data(msg.payload)
            else:
                # localized SPARTN; send to receiver
                self.gnss.write(msg.payload)
        else:
            logging.warning('Unhandled topic %s', msg.topic)


    def loop_forever(self):
        '''Main loop of the client.'''
        # avoid subscribing before fully connected (race in paho)
        while not self.connected:
            time.sleep(0.1)
        try:
            buffer = bytearray(100)
            while True:
                bytes_read = self.gnss.readinto(buffer)
                if bytes_read:
                    if self.ubxfile:
                        self.ubxfile.write(buffer[0:bytes_read])
                    # parse the bytes and invoke matching handlers
                    self.nmea_parser.parse(buffer[0:bytes_read])
        finally:
            logging.info('Disconnecting from %s', self.mqtt_server)
            self.mqtt_client.disconnect()
            while self.connected:
                time.sleep(0.1)
            self.mqtt_client.loop_stop()

    def handle_nmea_gga(self, sentence):
        '''Process an NMEA-GGA sentence passed in as a string.'''
        logging.info(sentence)
        fields = sentence.split(',')
        quality = int(fields[6] or 0)
        f_lat = float(fields[2] or 0)
        lat = int(f_lat / 100) + (f_lat % 100) / 60
        if fields[3] == 'S':
            lat *= -1
        f_lon = float(fields[4] or 0)
        lon = int(f_lon / 100) + (f_lon % 100) / 60
        if fields[5] == 'W':
            lon *= -1

        if self.stats:
            self.stats.epochs[quality] += 1
            self.stats.total += 1
            if self.stats.total % self.stats.interval == 0:
                pct = [f'{QUALITIES[i]}: {self.stats.epochs[i] / self.stats.total * 100:.1f}%'
                        for i in range(len(QUALITIES)) if self.stats.epochs[i]]
                logging.log(STATS, ', '.join(pct))

        if quality in (0, 6):  # no fix or estimated
            if not self.assist_now_topic:
                self.assist_now_topic = '/pp/ubx/mga'
                logging.info('Subscribing to %s', self.assist_now_topic)
                self.mqtt_client.subscribe((self.assist_now_topic, 1))
        else:
            if self.assist_now_topic and not self.assist_now:
                logging.info('Unsubscribing from %s', self.assist_now_topic)
                self.mqtt_client.unsubscribe(self.assist_now_topic)
                self.assist_now_topic = None
            self.process_position(lat, lon)


    def process_position(self, lat, lon):
        '''Handle position from the receiver. If needed, subscribe to a new tile
           or topic.'''
        if self.localized:
            self.epoch_count += 1
            # Only record new position if it changed significantly since the last calculation
            if abs(lat - self.lat) > self.dlat_threshold or \
            abs(lon - self.lon) > self.dlon_threshold or \
            self.epoch_count > self.epochs:
                logging.debug('updating position: %f, %f', lat, lon)
                self.lat = lat
                self.lon = lon
                self.epoch_count = 0
                self.dlon_threshold = self.dlat_threshold * cos(radians(self.lat))
                new_tile_topic = self.get_tile_topic(self.lat, self.lon)
                if new_tile_topic != self.tile_topic:
                    if self.tile_topic:
                        self.mqtt_client.unsubscribe(self.tile_topic)
                    logging.info('Subscribing to tile %s', new_tile_topic)
                    self.mqtt_client.subscribe((new_tile_topic, 1))
                    self.tile_topic = new_tile_topic
                    # the incoming tile data will trigger node selection
                else:
                    self.select_node()
        else:
            if not self.spartn_topic:
                logging.debug('updating position: %f, %f', lat, lon)
                self.lat = lat
                self.lon = lon
                # Fake tile dictionary for regional mode, allowing automatic
                # selection of the region
                self.tile_dict = { 'nodes': REGION_MAPPING.keys(),
                                   'nodeprefix': f'/pp/{self.plan}/',
                                   'endpoint': self.mqtt_server }
                self.select_node()


    def select_node(self):
        '''Select the closest node to the current position.'''
        if not self.tile_dict:
            # not yet ready to select a node, as we don't have tile data
            return
        # Rather than calculate distance in meters, calculate a value that grows with
        # the distance, since all we care about is finding the closest.
        # As an approximation, use the sum of the lat and lon difference
        # squared, but scale the latitude difference by cos(lon) to make it the
        # same scale as longitude.
        rounded_lat = round(self.lat * 100)
        rounded_lon = round(self.lon * 100)
        factor_lon = cos(radians(self.lat))
        min_dist_scaled = float('inf')
        for node in self.tile_dict['nodes']:
            node_signed = node.translate(self.NSEW_TO_SIGN)
            node_lat = int(node_signed[0:5])
            node_lon = int(node_signed[5:11])
            # longitude difference is proportional to distance along NS
            # latitude difference is proportional to distance along EW,
            # but scale by cos(lon) to make it the same scale as lon
            dist_scaled = (node_lat-rounded_lat)**2 + ((node_lon-rounded_lon)*factor_lon)**2
            if dist_scaled < min_dist_scaled:
                min_dist_scaled = dist_scaled
                nearest_node = node
        if self.localized:
            logging.debug('Nearest node: %s', nearest_node)
        else:
            # we are trying to determine a continental topic name
            # replace the node name with the region name
            nearest_node = REGION_MAPPING[nearest_node]
            logging.warning('Region "%s" automatically detected', nearest_node)
        if self.mqtt_server != self.tile_dict['endpoint']:
            # store new server and correction topic; on completion of the
            # disconnect below, the disconnect callback will initiate the new
            # connection and the connect callback will subscribe to the new
            # topic
            self.new_server = self.tile_dict['endpoint']
            self.spartn_topic = self.tile_dict['nodeprefix'] + nearest_node
            self.mqtt_client.disconnect()
        else:
            if self.spartn_topic:
                self.mqtt_client.unsubscribe(self.spartn_topic)
            self.spartn_topic = self.tile_dict['nodeprefix'] + nearest_node
            logging.info('Subscribing to topic %s', self.spartn_topic)
            self.mqtt_client.subscribe((self.spartn_topic, 0))


    def get_tile_topic(self, lat, lon):
        '''Get the MQTT topic for the tile containing the given position.'''
        delta = [10.0, 5.0, 2.5][self.tile_level]
        n_s = 'S' if lat < 0 else 'N'
        e_w = 'W' if lon < 0 else 'E'
        # Get the lower left corner of the tile in latitude and longitude
        llat = floor(lat / delta) * delta
        llon = floor(lon / delta) * delta
        # Shift to the center of the tile
        clat = llat + (delta / 2)
        clon = llon + (delta / 2)
        # Multiply by 100, round to the nearest integer, remove sign
        slat = abs(round(clat * 100))
        slon = abs(round(clon * 100))
        return f'pp/ip/L{self.tile_level}{n_s}{slat:04d}{e_w}{slon:05d}/dict'


    def process_tile_data(self, data):
        '''Process MQTT tile data.'''
        try:
            self.tile_dict = json.loads(data)
        except json.JSONDecodeError:
            assert False, 'Invalid JSON data received for tile'
        self.select_node()
