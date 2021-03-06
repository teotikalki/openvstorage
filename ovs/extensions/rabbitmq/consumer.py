#!/usr/bin/env python2
# Copyright 2014 Open vStorage NV
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Consumes messages from rabbitmq, dispatching them to the process method and acknowledges them
"""

import pika
import os
import imp
import inspect
import time
import sys
import logging
import pyinotify
from ConfigParser import RawConfigParser
from ovs.extensions.rabbitmq.processor import process
from ovs.extensions.generic.system import System
from ovs.extensions.generic.configuration import Configuration
from ovs.log.logHandler import LogHandler

logger = LogHandler.get('extensions', name='consumer')
logging.basicConfig()
KVM_ETC = '/etc/libvirt/qemu/'
KVM_RUN = '/run/libvirt/qemu/'

mapping = {}


def run_kvm_watcher():
    """
    Check whether to run the KVM file watcher
    """
    return System.get_my_storagerouter().pmachine.hvtype == 'KVM'


def run_event_consumer():
    """
    Check whether to run the event consumer
    """
    rmq_config = RawConfigParser()
    rmq_config.read(os.path.join(Configuration.get('ovs.core.cfgdir'), 'rabbitmqclient.cfg'))
    machine_id = System.get_my_machine_id()
    return rmq_config.has_section(machine_id)


def callback(ch, method, properties, body):
    """
    Handles the message, making sure it gets acknowledged once processed
    """
    _ = properties
    try:
        process(queue, body, mapping)
    except Exception as e:
        logger.exception('Error processing message: {0}'.format(e))
    ch.basic_ack(delivery_tag=method.delivery_tag)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description = 'KVM File Watcher and Rabbitmq Event Processor for OVS',
                                     formatter_class = argparse.RawDescriptionHelpFormatter)

    parser.add_argument('rabbitmq_queue', type=str,
                        help='Rabbitmq queue name')
    parser.add_argument('--durable', dest='queue_durable', action='store_const', default=False, const=True,
                        help='Declare queue as durable')
    parser.add_argument('--watcher', dest='file_watcher', action='store_const', default=False, const=True,
                        help='Enable file watcher')

    args = parser.parse_args()
    print(args.rabbitmq_queue, args.queue_durable, args.file_watcher)
    notifier = None
    try:
        if args.file_watcher and run_kvm_watcher():
            from ovs.extensions.rabbitmq.kvm_xml_processor import Kxp

            wm = pyinotify.WatchManager()

            ETC_MASK_EVENTS_TO_WATCH = pyinotify.IN_CLOSE_WRITE | \
                pyinotify.IN_CREATE | \
                pyinotify.IN_DELETE | \
                pyinotify.IN_MODIFY | \
                pyinotify.IN_MOVED_FROM | \
                pyinotify.IN_MOVED_TO | \
                pyinotify.IN_UNMOUNT

            RUN_MASK_EVENTS_TO_WATCH = pyinotify.IN_DELETE | \
                pyinotify.IN_MOVED_TO

            notifier = pyinotify.ThreadedNotifier(wm, Kxp())
            notifier.start()

            _ = wm.add_watch(KVM_ETC, ETC_MASK_EVENTS_TO_WATCH, rec=True)
            logger.info('Watching {0}...'.format(KVM_ETC), print_msg=True)
            _ = wm.add_watch(KVM_RUN, RUN_MASK_EVENTS_TO_WATCH, rec=True)
            logger.info('Watching {0}...'.format(KVM_RUN), print_msg=True)
            logger.info('KVM xml processor active...', print_msg=True)

        if run_event_consumer():
            # Load mapping
            mapping = {}
            path = os.path.join(os.path.dirname(__file__), 'mappings')
            for filename in os.listdir(path):
                if os.path.isfile(os.path.join(path, filename)) and filename.endswith('.py'):
                    name = filename.replace('.py', '')
                    module = imp.load_source(name, os.path.join(path, filename))
                    for member in inspect.getmembers(module):
                        if inspect.isclass(member[1]) \
                                and member[1].__module__ == name \
                                and 'object' in [base.__name__ for base in member[1].__bases__]:
                            this_mapping = member[1].mapping
                            for key in this_mapping.keys():
                                if key not in mapping:
                                    mapping[key] = []
                                mapping[key] += this_mapping[key]
            logger.debug('Event map:')
            for key in mapping:
                logger.debug('{0}: {1}'.format(key.name, [current_map['task'].__name__ for current_map in mapping[key]]))

            # Starting connection and handling
            rmq_ini = RawConfigParser()
            rmq_ini.read(os.path.join(Configuration.get('ovs.core.cfgdir'), 'rabbitmqclient.cfg'))
            rmq_nodes = [node.strip() for node in rmq_ini.get('main', 'nodes').split(',')]
            this_server = '{0}:{1}'.format(Configuration.get('ovs.grid.ip'), Configuration.get('ovs.core.broker.port'))
            rmq_servers = [this_server] + [server for server in map(lambda n: rmq_ini.get(n, 'location'), rmq_nodes) if server != this_server]
            channel = None
            server = ''
            loglevel = logging.root.manager.disable  # Workaround for disabling logging
            logging.disable('WARNING')
            for server in rmq_servers:
                try:
                    connection = pika.BlockingConnection(
                        pika.ConnectionParameters(host=server.split(':')[0],
                                                  port=int(server.split(':')[1]),
                                                  credentials=pika.PlainCredentials(
                                                      Configuration.get('ovs.core.broker.login'),
                                                      Configuration.get('ovs.core.broker.password'))
                                                  )
                    )
                    channel = connection.channel()
                    break
                except:
                    pass
            logging.disable(loglevel)  # Restore workaround
            if channel is None:
                raise RuntimeError('Could not connect to any available RabbitMQ endpoint.')
            logger.debug('Connected to: {0}'.format(server))

            queue = args.rabbitmq_queue
            durable = args.queue_durable
            channel.queue_declare(queue=queue, durable=durable)
            logger.info('Waiting for messages on {0}...'.format(queue), print_msg=True)
            logger.info('To exit press CTRL+C', print_msg=True)

            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(callback, queue=queue)
            channel.start_consuming()
        else:
            # We need to keep the process running
            logger.info('To exit press CTRL+C', print_msg=True)
            while True:
                time.sleep(3600)

    except KeyboardInterrupt:
        if run_kvm_watcher() and notifier is not None:
            notifier.stop()
    except Exception as exception:
        logger.error('Unexpected exception: {0}'.format(str(exception)), print_msg=True)
        if run_kvm_watcher() and notifier is not None:
            notifier.stop()
        sys.exit(1)
