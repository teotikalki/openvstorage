# Copyright 2015 Open vStorage NV
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

import sys
from ovs.extensions.db.arakoon.ArakoonInstaller import ArakoonInstaller
from ovs.extensions.storage.persistentfactory import PersistentFactory
from ovs.extensions.generic.configuration import Configuration
from ovs.extensions.generic.system import System
from ovs.extensions.generic.sshclient import SSHClient
from unittest import TestCase


class TestArakoonInstaller(TestCase):

    nodes = None
    expected_global = None
    expected_base = None

    @classmethod
    def setUpClass(cls):
        ArakoonInstaller.ARAKOON_CONFIG_DIR = '/tmp/cfg'
        ArakoonInstaller.ARAKOON_CONFIG_FILE = '/tmp/cfg/{0}/{0}.cfg'

        TestArakoonInstaller.expected_global = '[global]\ncluster_id = {0}\ncluster = {1}\nplugins = \n\n'
        TestArakoonInstaller.expected_base = '[{0}]\nname = {0}\nip = {1}\nclient_port = {2}\nmessaging_port = {3}\ntlog_compression = snappy\nlog_level = info\nlog_dir = /var/log/arakoon/one\nhome = /tmp/db/arakoon/one\ntlog_dir = /tmp/db/tlogs/one\nfsync = true\n\n'

        # System
        def _get_my_machine_id(_client):
            return TestArakoonInstaller.nodes[_client.ip]

        System.get_my_machine_id = staticmethod(_get_my_machine_id)

        # Configuration
        def _get(key):
            if key == 'ovs.core.storage.persistent':
                return 'arakoon'
            c = PersistentFactory.get_client()
            if c.exists(key):
                return c.get(key)
            return None

        def _get_int(key):
            return int(Configuration.get(key))

        def _set(key, value):
            c = PersistentFactory.get_client()
            c.set(key, value)

        Configuration.get = staticmethod(_get)
        Configuration.get_int = staticmethod(_get_int)
        Configuration.set = staticmethod(_set)

        Configuration.set('ovs.ports.arakoon', [22000])
        Configuration.set('ovs.arakoon.location', '/tmp/db')

    @classmethod
    def setUp(cls):
        for node in TestArakoonInstaller.nodes:
            client = SSHClient(node)
            client.dir_delete('/tmp/db')
            client.dir_delete('/tmp/cfg')
            client.dir_create('/tmp/db')
            client.dir_create('/tmp/cfg')

    def _get_config_path(self, cluster):
        return '/tmp/cfg/{0}/{0}.cfg'.format(cluster)

    def test_single_node(self):
        base_port = Configuration.get('ovs.ports.arakoon')[0]
        cluster = 'one'
        node = sorted(TestArakoonInstaller.nodes.keys())[0]
        ArakoonInstaller.create_cluster(cluster, node, [])
        contents = SSHClient(node).file_read(self._get_config_path(cluster))
        expected = TestArakoonInstaller.expected_global.format(cluster, TestArakoonInstaller.nodes[node])
        expected += TestArakoonInstaller.expected_base.format(TestArakoonInstaller.nodes[node], node, base_port, base_port + 1)
        self.assertEqual(contents.strip(), expected.strip())

    def test_multi_node(self):
        base_port = Configuration.get('ovs.ports.arakoon')[0]
        cluster = 'one'
        nodes = sorted(TestArakoonInstaller.nodes.keys())
        nodes = dict((node, SSHClient(node)) for node in nodes)
        first_node = nodes.keys()[0]
        ArakoonInstaller.create_cluster(cluster, first_node, [])
        for node in nodes[1:]:
            ArakoonInstaller.extend_cluster(first_node, node, cluster, [])
        expected = TestArakoonInstaller.expected_global.format(cluster, ','.join(TestArakoonInstaller.nodes[node] for node in nodes))
        for node in nodes:
            expected += TestArakoonInstaller.expected_base.format(TestArakoonInstaller.nodes[node], node, base_port, base_port + 1)
        expected = expected.strip()
        for node, client in nodes.iteritems():
            contents = client.file_read(self._get_config_path(cluster))
            self.assertEqual(contents.strip(), expected.strip())
        ArakoonInstaller.shrink_cluster(nodes[1], first_node, cluster)
        expected = TestArakoonInstaller.expected_global.format(cluster, ','.join(TestArakoonInstaller.nodes[node] for node in nodes[1:]))
        for node in nodes.keys()[1:]:
            expected += TestArakoonInstaller.expected_base.format(TestArakoonInstaller.nodes[node], node, base_port, base_port + 1)
        expected = expected.strip()
        for node, client in nodes.iteritems():
            if node == first_node:
                continue
            contents = client.file_read(self._get_config_path(cluster))
            self.assertEqual(contents.strip(), expected.strip())


if __name__ == '__main__':
    import unittest
    TestArakoonInstaller.nodes = {}
    if len(sys.argv) == 1:
        print 'Please specify the IP addresses of the OVS nodes to run these tests on.'
        print 'Make sure you have all your code-to-be-tested copied to these nodes'
    for arg in sys.argv[1:]:
        TestArakoonInstaller.nodes[arg] = arg.replace('.', '')
    suite = unittest.TestLoader().loadTestsFromTestCase(TestArakoonInstaller)
    unittest.TextTestRunner(verbosity=2).run(suite)
