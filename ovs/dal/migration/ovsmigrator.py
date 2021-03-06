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
OVS migration module
"""

import hashlib
import random
import string


class OVSMigrator(object):
    """
    Handles all model related migrations
    """

    identifier = 'ovs'

    def __init__(self):
        """ Init method """
        pass

    @staticmethod
    def migrate(previous_version):
        """
        Migrates from any version to any version, running all migrations required
        If previous_version is for example 0 and this script is at
        verison 3 it will execute two steps:
          - 1 > 2
          - 2 > 3
        @param previous_version: The previous version from which to start the migration.
        """

        working_version = previous_version

        # Version 1 introduced:
        # - The datastore is still empty, add defaults
        if working_version < 1:
            from ovs.dal.hybrids.user import User
            from ovs.dal.hybrids.group import Group
            from ovs.dal.hybrids.role import Role
            from ovs.dal.hybrids.client import Client
            from ovs.dal.hybrids.j_rolegroup import RoleGroup
            from ovs.dal.hybrids.j_roleclient import RoleClient
            from ovs.dal.hybrids.backendtype import BackendType
            from ovs.dal.hybrids.servicetype import ServiceType
            from ovs.dal.hybrids.branding import Branding
            from ovs.dal.lists.backendtypelist import BackendTypeList

            # Create groups
            admin_group = Group()
            admin_group.name = 'administrators'
            admin_group.description = 'Administrators'
            admin_group.save()
            viewers_group = Group()
            viewers_group.name = 'viewers'
            viewers_group.description = 'Viewers'
            viewers_group.save()

            # Create users
            admin = User()
            admin.username = 'admin'
            admin.password = hashlib.sha256('admin').hexdigest()
            admin.is_active = True
            admin.group = admin_group
            admin.save()

            # Create internal OAuth 2 clients
            admin_pw_client = Client()
            admin_pw_client.ovs_type = 'INTERNAL'
            admin_pw_client.grant_type = 'PASSWORD'
            admin_pw_client.user = admin
            admin_pw_client.save()
            admin_cc_client = Client()
            admin_cc_client.ovs_type = 'INTERNAL'
            admin_cc_client.grant_type = 'CLIENT_CREDENTIALS'
            admin_cc_client.client_secret = ''.join(random.choice(string.ascii_letters +
                                                                  string.digits +
                                                                  '|_=+*#@!/-[]{}<>.?,\'";:~')
                                                    for _ in range(128))
            admin_cc_client.user = admin
            admin_cc_client.save()

            # Create roles
            read_role = Role()
            read_role.code = 'read'
            read_role.name = 'Read'
            read_role.description = 'Can read objects'
            read_role.save()
            write_role = Role()
            write_role.code = 'write'
            write_role.name = 'Write'
            write_role.description = 'Can write objects'
            write_role.save()
            manage_role = Role()
            manage_role.code = 'manage'
            manage_role.name = 'Manage'
            manage_role.description = 'Can manage the system'
            manage_role.save()

            # Attach groups to roles
            mapping = [
                (admin_group, [read_role, write_role, manage_role]),
                (viewers_group, [read_role])
            ]
            for setting in mapping:
                for role in setting[1]:
                    rolegroup = RoleGroup()
                    rolegroup.group = setting[0]
                    rolegroup.role = role
                    rolegroup.save()
                for user in setting[0].users:
                    for role in setting[1]:
                        for client in user.clients:
                            roleclient = RoleClient()
                            roleclient.client = client
                            roleclient.role = role
                            roleclient.save()

            # Add backends
            for backend_type_info in [('Ceph', 'ceph_s3'), ('Amazon', 'amazon_s3'), ('Swift', 'swift_s3'),
                                      ('Local', 'local'), ('Distributed', 'distributed'), ('ALBA', 'alba')]:
                code = backend_type_info[1]
                backend_type = BackendTypeList.get_backend_type_by_code(code)
                if backend_type is None:
                    backend_type = BackendType()
                backend_type.name = backend_type_info[0]
                backend_type.code = code
                backend_type.save()

            # Add service types
            for service_type_info in ['MetadataServer', 'AlbaProxy', 'Arakoon']:
                service_type = ServiceType()
                service_type.name = service_type_info
                service_type.save()

            # Brandings
            branding = Branding()
            branding.name = 'Default'
            branding.description = 'Default bootstrap theme'
            branding.css = 'bootstrap-default.min.css'
            branding.productname = 'Open vStorage'
            branding.is_default = True
            branding.save()
            slate = Branding()
            slate.name = 'Slate'
            slate.description = 'Dark bootstrap theme'
            slate.css = 'bootstrap-slate.min.css'
            slate.productname = 'Open vStorage'
            slate.is_default = False
            slate.save()

            # We're now at version 1
            working_version = 1

        # Version 2 introduced:
        # - new Descriptor format
        if working_version < 2:
            import imp
            from ovs.dal.helpers import Descriptor
            from ovs.extensions.storage.persistentfactory import PersistentFactory

            client = PersistentFactory.get_client()
            keys = client.prefix('ovs_data', max_elements=-1)
            for key in keys:
                data = client.get(key)
                modified = False
                for entry in data.keys():
                    if isinstance(data[entry], dict) and 'source' in data[entry] and 'hybrids' in data[entry]['source']:
                        filename = data[entry]['source']
                        if not filename.startswith('/'):
                            filename = '/opt/OpenvStorage/ovs/dal/{0}'.format(filename)
                        module = imp.load_source(data[entry]['name'], filename)
                        cls = getattr(module, data[entry]['type'])
                        new_data = Descriptor(cls, cached=False).descriptor
                        if 'guid' in data[entry]:
                            new_data['guid'] = data[entry]['guid']
                        data[entry] = new_data
                        modified = True
                if modified is True:
                    data['_version'] += 1
                    client.set(key, data)

            # We're now at version 2
            working_version = 2

        # Version 3 introduced:
        # - new Descriptor format
        if working_version < 3:
            import imp
            from ovs.dal.helpers import Descriptor
            from ovs.extensions.storage.persistentfactory import PersistentFactory

            client = PersistentFactory.get_client()
            keys = client.prefix('ovs_data', max_elements=-1)
            for key in keys:
                data = client.get(key)
                modified = False
                for entry in data.keys():
                    if isinstance(data[entry], dict) and 'source' in data[entry]:
                        module = imp.load_source(data[entry]['name'], data[entry]['source'])
                        cls = getattr(module, data[entry]['type'])
                        new_data = Descriptor(cls, cached=False).descriptor
                        if 'guid' in data[entry]:
                            new_data['guid'] = data[entry]['guid']
                        data[entry] = new_data
                        modified = True
                if modified is True:
                    data['_version'] += 1
                    client.set(key, data)

            working_version = 3

        return working_version
