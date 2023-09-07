import argparse
import time
import logging
import sys
import json
import os
import tempfile
import re
import paho.mqtt.client as mqtt
from ros_publisher import RosPointPerfectPublisher
from point_perfect_client import PointPerfectClient

STATS = 100  # logging level for stats

KEY_HEADER = '-----BEGIN RSA PRIVATE KEY-----\n'
KEY_FOOTER = '\n-----END RSA PRIVATE KEY-----\n'
CERT_HEADER = '-----BEGIN CERTIFICATE-----\n'
CERT_FOOTER = '\n-----END CERTIFICATE-----\n'


def load_json_credentials(args, argp):
    '''Load MQTT credentials from a u-center Config JSON file.'''

    # verify that no conflicting arguments were given
    if (args.client_id or args.dir != argp.get_default('dir') or
         args.server != argp.get_default('server') or
         args.lband != argp.get_default('lband')):
        argp.error('Cannot use -j/--json with -i/--client_id, -d/--dir, -s/--server, or --lband')

    try:
        with open(args.json, 'r', encoding='utf-8') as json_file:
            json_data = json.load(json_file)
            json_file.close()
            conn = json_data['MQTT']['Connectivity']
            args.client_id = conn['ClientID']
            server_uri = conn['ServerURI']
            creds = conn['ClientCredentials']
            key = creds['Key']
            cert = creds['Cert']
            args.lband = '/pp/ubx/0236/Lb' in json_data['MQTT']['Subscriptions']['Key']['KeyTopics']
    except FileNotFoundError:
        argp.error(f'JSON file {args.json} not found')
    except json.JSONDecodeError:
        argp.error(f'JSON file {args.json} is not valid JSON')
    except KeyError as error:
        argp.error(f'JSON file {args.json} is missing key {error}')
    except (TypeError, ValueError):
        argp.error(f'JSON file {args.json} is not valid')

    # Parse the server URI
    match = re.match(r'(tcp|ssl)://(.+):(\d+)', server_uri)
    args.server = match.group(2)
    assert match.group(1) == 'ssl'
    assert match.group(3) == '8883'

    # Write the credentials to temporary files, as needed by paho.mqtt.client
    (keyf, args.keyfile) = tempfile.mkstemp(
                                prefix=f'device-{args.client_id}-', suffix='-pp-key.pem')
    os.write(keyf, f'{KEY_HEADER}{key}{KEY_FOOTER}'.encode('ascii'))
    os.close(keyf)
    (certf, args.certfile) = tempfile.mkstemp(
                                prefix=f'device-{args.client_id}-', suffix='-pp-cert.crt')
    os.write(certf, f'{CERT_HEADER}{cert}{CERT_FOOTER}'.encode('ascii'))
    os.close(certf)


def main():
    '''Main program.'''
    argp = argparse.ArgumentParser()
    argp.add_argument('-j', '--json', type=str,
        help='u-center JSON file containing MQTT credentials')
    argp.add_argument('--assistnow', action='store_true',
        help='Use AssistNow regardless of GNSS receiver state')

    s_group = argp.add_mutually_exclusive_group()
    s_group.add_argument('--region', default=None,
        help='Service region (e.g. us, eu), defaults to automatic detection')
    s_group.add_argument('-l', '--localized', action='store_true',
        help='Use localized service')

    o_group = argp.add_argument_group('Output options')
    time_stamp = time.strftime("%Y%m%d_%H%M%S")
    o_group.add_argument('-u', '--ubx', nargs='?', type=argparse.FileType('wb'),
        const=f'pointperfect_log_{time_stamp}.ubx',
        help='Write all GNSS receiver output to a UBX file')
    o_group.add_argument('--log', nargs='?', type=argparse.FileType('w'),
        const=f'pointperfect_log_{time_stamp}.txt',
        help='Write all program output to a text file in addition to stdout')
    o_group.add_argument('--stats', type=int, nargs='?', const=5, default=None,
        help='Print statistics every N epochs (default: off, 5 if no argument given)')
    o_group.add_argument('--trace', choices=('CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'),
                         default='INFO',
                         help='Trace level: CRITICAL, ERROR, WARNING, INFO, DEBUG (default: INFO)')

    cgroup = argp.add_argument_group('MQTT settings',
        description='These options apply only when NOT using -j/--json. '
                    'Otherwise, the values are read from the JSON file.')
    cgroup.add_argument('-i', '--client_id',
        help='The MQTT client ID to use')
    cgroup.add_argument('-d', '--dir', default='.',
        help='Directory containing key and certificate files (default: .)')
    cgroup.add_argument('-s', '--server', default='pp.services.u-blox.com',
        help='MQTT server address (default: pp.services.u-blox.com)')
    cgroup.add_argument('--lband', action='store_true',
        help='Use MQTT topics suitable for devices on an Lband+IP combined plan')

    lgroup = argp.add_argument_group('Localized options',
        description='These options apply only in combination with --localized')
    lgroup.add_argument('--distance', default=50000, type=int,
        help='The distance threshold [m] for recalculating tile and node (default: 50000)')
    lgroup.add_argument('--epochs', default=float('inf'), type=float,
        help='The maximum number of epochs between recalculating tile and node (default: infinite)')
    lgroup.add_argument('-L', '--tile-level', type=int, choices=(0,1,2), default=2,
        help='Tile level for localized service (default: 2)')
    args = argp.parse_args()

    logging.basicConfig(level=getattr(logging, args.trace),
                        format='%(levelname)s %(message)s',
                        stream=sys.stdout)
    if args.log:
        logging.getLogger().addHandler(logging.FileHandler(args.log.name))
    logging.info(' '.join(sys.argv))  # log the command line arguments
    logging.addLevelName(STATS, 'STATS')

    if not args.localized:
        if args.distance != argp.get_default('distance'):
            argp.error('--distance requires --localized')
        if args.epochs != argp.get_default('epochs'):
            argp.error('--epochs requires --localized')
        if args.tile_level != argp.get_default('tile_level'):
            argp.error('--tile-level requires --localized')

    try:
        if args.json:
            load_json_credentials(args, argp)
        else:
            if not args.client_id:
                argp.error('Either -j/--json or -i/--client_id must be specified')
            args.certfile = os.path.join(args.dir, f'device-{args.client_id}-pp-cert.crt')
            args.keyfile  = os.path.join(args.dir, f'device-{args.client_id}-pp-key.pem')
            if not os.path.exists(args.certfile):
                argp.error(f'Certificate file {args.certfile} does not exist')
            if not os.path.exists(args.keyfile):
                argp.error(f'Key file {args.keyfile} does not exist')

        mqtt_client = mqtt.Client(client_id=args.client_id)
        mqtt_client.tls_set(certfile=args.certfile, keyfile=args.keyfile)
        mqtt_client.enable_logger()
    finally:
        if args.json:
            # remove the temporary key/cert as early as possible
            # they do get loaded in tls_set() and are not used thereafter
            if 'certfile' in args and os.path.exists(args.certfile):
                os.remove(args.certfile)
            if 'keyfile' in args and os.path.exists(args.keyfile):
                os.remove(args.keyfile)

    if args.ubx:
        logging.info('Writing all receiver data to %s', args.ubx.name)

    publisher = RosPointPerfectPublisher()

    try:
        pp_client = PointPerfectClient(publisher, mqtt_client, args.server, 8883,
                        localized=args.localized, lband=args.lband, region=args.region,
                        tile_level=args.tile_level, distance=args.distance, epochs=args.epochs,
                        ubxfile=args.ubx, stats=args.stats, assist_now=args.assistnow)
        pp_client.loop_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
