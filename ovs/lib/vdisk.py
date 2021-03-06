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
Module for VDiskController
"""
import pickle
import uuid
import os
import time
from subprocess import check_output

from ovs.lib.helpers.decorators import log
from ovs.celery_run import celery
from ovs.dal.hybrids.vdisk import VDisk
from ovs.dal.hybrids.vmachine import VMachine
from ovs.dal.hybrids.pmachine import PMachine
from ovs.dal.hybrids.storagedriver import StorageDriver
from ovs.dal.lists.vdisklist import VDiskList
from ovs.dal.lists.storagedriverlist import StorageDriverList
from ovs.dal.lists.vpoollist import VPoolList
from ovs.dal.lists.pmachinelist import PMachineList
from ovs.dal.lists.mgmtcenterlist import MgmtCenterList
from ovs.dal.hybrids.vpool import VPool
from ovs.extensions.hypervisor.factory import Factory
from ovs.extensions.storageserver.storagedriver import StorageDriverClient
from ovs.log.logHandler import LogHandler
from ovs.lib.mdsservice import MDSServiceController
from ovs.extensions.generic.volatilemutex import VolatileMutex
from volumedriver.storagerouter import storagerouterclient
from volumedriver.storagerouter.storagerouterclient import MDSMetaDataBackendConfig, MDSNodeConfig

logger = LogHandler.get('lib', name='vdisk')
storagerouterclient.Logger.setupLogging(LogHandler.load_path('storagerouterclient'))
storagerouterclient.Logger.enableLogging()


class VDiskController(object):
    """
    Contains all BLL regarding VDisks
    """

    @staticmethod
    @celery.task(name='ovs.vdisk.list_volumes')
    def list_volumes(vpool_guid=None):
        """
        List all known volumes on a specific vpool or on all
        """
        if vpool_guid is not None:
            vpool = VPool(vpool_guid)
            storagedriver_client = StorageDriverClient.load(vpool)
            response = storagedriver_client.list_volumes()
        else:
            response = []
            for vpool in VPoolList.get_vpools():
                storagedriver_client = StorageDriverClient.load(vpool)
                response.extend(storagedriver_client.list_volumes())
        return response

    @staticmethod
    @celery.task(name='ovs.vdisk.delete_from_voldrv')
    @log('VOLUMEDRIVER_TASK')
    def delete_from_voldrv(volumename, storagedriver_id):
        """
        Delete a disk
        Triggered by volumedriver messages on the queue
        @param volumename: volume id of the disk
        """
        _ = storagedriver_id  # For logging purposes
        disk = VDiskList.get_vdisk_by_volume_id(volumename)
        if disk is not None:
            mutex = VolatileMutex('{}_{}'.format(volumename, disk.devicename))
            try:
                mutex.acquire(wait=20)
                pmachine = None
                try:
                    pmachine = PMachineList.get_by_storagedriver_id(disk.storagedriver_id)
                except RuntimeError as ex:
                    if 'could not be found' not in str(ex):
                        raise
                    # else: pmachine can't be loaded, because the volumedriver doesn't know about it anymore
                if pmachine is not None:
                    limit = 5
                    storagedriver = StorageDriverList.get_by_storagedriver_id(storagedriver_id)
                    hypervisor = Factory.get(pmachine)
                    exists = hypervisor.file_exists(storagedriver, disk.devicename)
                    while limit > 0 and exists is True:
                        time.sleep(1)
                        exists = hypervisor.file_exists(storagedriver, disk.devicename)
                        limit -= 1
                    if exists is True:
                        logger.info('Disk {0} still exists, ignoring delete'.format(disk.devicename))
                        return
                logger.info('Delete disk {}'.format(disk.name))
                for mds_service in disk.mds_services:
                    mds_service.delete()
                disk.delete()
            finally:
                mutex.release()

    @staticmethod
    @celery.task(name='ovs.vdisk.resize_from_voldrv')
    @log('VOLUMEDRIVER_TASK')
    def resize_from_voldrv(volumename, volumesize, volumepath, storagedriver_id):
        """
        Resize a disk
        Triggered by volumedriver messages on the queue

        @param volumepath: path on hypervisor to the volume
        @param volumename: volume id of the disk
        @param volumesize: size of the volume
        """
        pmachine = PMachineList.get_by_storagedriver_id(storagedriver_id)
        storagedriver = StorageDriverList.get_by_storagedriver_id(storagedriver_id)
        hypervisor = Factory.get(pmachine)
        volumepath = hypervisor.clean_backing_disk_filename(volumepath)
        mutex = VolatileMutex('{}_{}'.format(volumename, volumepath))
        try:
            mutex.acquire(wait=30)
            disk = VDiskList.get_vdisk_by_volume_id(volumename)
            if disk is None:
                disk = VDiskList.get_by_devicename_and_vpool(volumepath, storagedriver.vpool)
                if disk is None:
                    disk = VDisk()
        finally:
            mutex.release()
        disk.devicename = volumepath
        disk.volume_id = volumename
        disk.size = volumesize
        disk.vpool = storagedriver.vpool
        disk.save()
        VDiskController.sync_with_mgmtcenter(disk, pmachine, storagedriver)
        MDSServiceController.ensure_safety(disk)

    @staticmethod
    @celery.task(name='ovs.vdisk.rename_from_voldrv')
    @log('VOLUMEDRIVER_TASK')
    def rename_from_voldrv(volumename, volume_old_path, volume_new_path, storagedriver_id):
        """
        Rename a disk
        Triggered by volumedriver messages

        @param volumename: volume id of the disk
        @param volume_old_path: old path on hypervisor to the volume
        @param volume_new_path: new path on hypervisor to the volume
        """
        pmachine = PMachineList.get_by_storagedriver_id(storagedriver_id)
        hypervisor = Factory.get(pmachine)
        volume_old_path = hypervisor.clean_backing_disk_filename(volume_old_path)
        volume_new_path = hypervisor.clean_backing_disk_filename(volume_new_path)
        disk = VDiskList.get_vdisk_by_volume_id(volumename)
        if disk:
            logger.info('Move disk {} from {} to {}'.format(disk.name,
                                                            volume_old_path,
                                                            volume_new_path))
            disk.devicename = volume_new_path
            disk.save()

    @staticmethod
    @celery.task(name='ovs.vdisk.clone')
    def clone(diskguid, snapshotid, devicename, pmachineguid, machinename, machineguid=None):
        """
        Clone a disk
        """
        pmachine = PMachine(pmachineguid)
        hypervisor = Factory.get(pmachine)
        description = '{} {}'.format(machinename, devicename)
        properties_to_clone = ['description', 'size', 'type', 'retentionpolicyguid',
                               'snapshotpolicyguid', 'autobackup']
        vdisk = VDisk(diskguid)
        location = hypervisor.get_backing_disk_path(machinename, devicename)

        new_vdisk = VDisk()
        new_vdisk.copy(vdisk, include=properties_to_clone)
        new_vdisk.parent_vdisk = vdisk
        new_vdisk.name = '{0}-clone'.format(vdisk.name)
        new_vdisk.description = description
        new_vdisk.devicename = hypervisor.clean_backing_disk_filename(location)
        new_vdisk.parentsnapshot = snapshotid
        new_vdisk.vmachine = VMachine(machineguid) if machineguid else vdisk.vmachine
        new_vdisk.vpool = vdisk.vpool
        new_vdisk.save()

        try:
            storagedriver = StorageDriverList.get_by_storagedriver_id(vdisk.storagedriver_id)
            if storagedriver is None:
                raise RuntimeError('Could not find StorageDriver with id {0}'.format(vdisk.storagedriver_id))

            mds_service = MDSServiceController.get_preferred_mds(storagedriver.storagerouter, vdisk.vpool)
            if mds_service is None:
                raise RuntimeError('Could not find a MDS service')

            logger.info('Clone snapshot {} of disk {} to location {}'.format(snapshotid, vdisk.name, location))
            volume_id = vdisk.storagedriver_client.create_clone(
                target_path=location,
                metadata_backend_config=MDSMetaDataBackendConfig([MDSNodeConfig(address=str(mds_service.service.storagerouter.ip),
                                                                                port=mds_service.service.ports[0])]),
                parent_volume_id=str(vdisk.volume_id),
                parent_snapshot_id=str(snapshotid),
                node_id=str(vdisk.storagedriver_id)
            )
        except Exception as ex:
            logger.error('Caught exception during clone, trying to delete the volume. {0}'.format(ex))
            new_vdisk.delete()
            VDiskController.delete_volume(location)
            raise

        new_vdisk.volume_id = volume_id
        new_vdisk.save()

        try:
            MDSServiceController.ensure_safety(new_vdisk)
        except Exception as ex:
            logger.error('Caught exception during "ensure_safety" {0}'.format(ex))

        return {'diskguid': new_vdisk.guid,
                'name': new_vdisk.name,
                'backingdevice': location}

    @staticmethod
    @celery.task(name='ovs.vdisk.create_snapshot')
    def create_snapshot(diskguid, metadata, snapshotid=None):
        """
        Create a disk snapshot

        @param diskguid: guid of the disk
        @param metadata: dict of metadata
        """
        disk = VDisk(diskguid)
        logger.info('Create snapshot for disk {}'.format(disk.name))
        if snapshotid is None:
            snapshotid = str(uuid.uuid4())
        metadata = pickle.dumps(metadata)
        disk.storagedriver_client.create_snapshot(
            str(disk.volume_id),
            snapshot_id=snapshotid,
            metadata=metadata
        )
        disk.invalidate_dynamics(['snapshots'])
        return snapshotid

    @staticmethod
    @celery.task(name='ovs.vdisk.delete_snapshot')
    def delete_snapshot(diskguid, snapshotid):
        """
        Delete a disk snapshot

        @param diskguid: guid of the disk
        @param snapshotid: id of the snapshot

        @todo: Check if new volumedriver storagedriver upon deletion
        of a snapshot has built-in protection to block it from being deleted
        if a clone was created from it.
        """
        disk = VDisk(diskguid)
        logger.info('Deleting snapshot {} from disk {}'.format(snapshotid, disk.name))
        disk.storagedriver_client.delete_snapshot(str(disk.volume_id), str(snapshotid))
        disk.invalidate_dynamics(['snapshots'])

    @staticmethod
    @celery.task(name='ovs.vdisk.set_as_template')
    def set_as_template(diskguid):
        """
        Set a disk as template

        @param diskguid: guid of the disk
        """
        disk = VDisk(diskguid)
        disk.storagedriver_client.set_volume_as_template(str(disk.volume_id))

    @staticmethod
    @celery.task(name='ovs.vdisk.rollback')
    def rollback(diskguid, timestamp):
        """
        Rolls back a disk based on a given disk snapshot timestamp
        """
        disk = VDisk(diskguid)
        snapshots = [snap for snap in disk.snapshots if snap['timestamp'] == timestamp]
        if not snapshots:
            raise ValueError('No snapshot found for timestamp {}'.format(timestamp))
        snapshotguid = snapshots[0]['guid']
        disk.storagedriver_client.rollback_volume(str(disk.volume_id), snapshotguid)
        disk.invalidate_dynamics(['snapshots'])
        return True

    @staticmethod
    @celery.task(name='ovs.vdisk.create_from_template')
    def create_from_template(diskguid, machinename, devicename, pmachineguid, machineguid=None, storagedriver_guid=None):
        """
        Create a disk from a template

        @param devicename: device file name for the disk (eg: mydisk-flat.vmdk)
        @param machineguid: guid of the machine to assign disk to
        @return diskguid: guid of new disk
        """

        pmachine = PMachine(pmachineguid)
        hypervisor = Factory.get(pmachine)
        disk_path = hypervisor.get_disk_path(machinename, devicename)

        description = '{} {}'.format(machinename, devicename)
        properties_to_clone = [
            'description', 'size', 'type', 'retentionpolicyid',
            'snapshotpolicyid', 'vmachine', 'vpool']

        vdisk = VDisk(diskguid)
        if vdisk.vmachine and not vdisk.vmachine.is_vtemplate:
            # Disk might not be attached to a vmachine, but still be a template
            raise RuntimeError('The given vdisk does not belong to a template')

        if storagedriver_guid is not None:
            storagedriver_id = StorageDriver(storagedriver_guid).storagedriver_id
        else:
            storagedriver_id = vdisk.storagedriver_id
        storagedriver = StorageDriverList.get_by_storagedriver_id(storagedriver_id)
        if storagedriver is None:
            raise RuntimeError('Could not find StorageDriver with id {0}'.format(storagedriver_id))

        new_vdisk = VDisk()
        new_vdisk.copy(vdisk, include=properties_to_clone)
        new_vdisk.vpool = vdisk.vpool
        new_vdisk.devicename = hypervisor.clean_backing_disk_filename(disk_path)
        new_vdisk.parent_vdisk = vdisk
        new_vdisk.name = '{}-clone'.format(vdisk.name)
        new_vdisk.description = description
        new_vdisk.vmachine = VMachine(machineguid) if machineguid else vdisk.vmachine
        new_vdisk.save()

        mds_service = MDSServiceController.get_preferred_mds(storagedriver.storagerouter, vdisk.vpool)
        if mds_service is None:
            raise RuntimeError('Could not find a MDS service')

        logger.info('Create disk from template {} to new disk {} to location {}'.format(
            vdisk.name, new_vdisk.name, disk_path
        ))
        try:
            volume_id = vdisk.storagedriver_client.create_clone_from_template(
                target_path=disk_path,
                metadata_backend_config=MDSMetaDataBackendConfig([MDSNodeConfig(address=str(mds_service.service.storagerouter.ip),
                                                                                port=mds_service.service.ports[0])]),
                parent_volume_id=str(vdisk.volume_id),
                node_id=str(storagedriver_id)
            )
            new_vdisk.volume_id = volume_id
            new_vdisk.save()
            MDSServiceController.ensure_safety(new_vdisk)

        except Exception as ex:
            logger.error('Clone disk on volumedriver level failed with exception: {0}'.format(str(ex)))
            new_vdisk.delete()
            raise

        return {'diskguid': new_vdisk.guid, 'name': new_vdisk.name,
                'backingdevice': disk_path}

    @staticmethod
    @celery.task(name='ovs.vdisk.create_volume')
    def create_volume(location, size):
        """
        Create a volume using filesystem calls
        Calls "truncate" to create sparse raw file
        TODO: use volumedriver API
        TODO: model VDisk() and return guid

        @param location: location, filename
        @param size: size of volume, GB
        @return None
        """
        if os.path.exists(location):
            raise RuntimeError('File already exists at %s' % location)

        output = check_output('truncate -s {0}G "{1}"'.format(size, location), shell=True).strip()
        output = output.replace('\xe2\x80\x98', '"').replace('\xe2\x80\x99', '"')

        if not os.path.exists(location):
            raise RuntimeError('Cannot create file %s. Output: %s' % (location, output))

    @staticmethod
    @celery.task(name='ovs.vdisk.delete_volume')
    def delete_volume(location):
        """
        Create a volume using filesystem calls
        Calls "rm" to delete raw file
        TODO: use volumedriver API
        TODO: delete VDisk from model

        @param location: location, filename
        @return None
        """
        if not os.path.exists(location):
            logger.error('File already deleted at %s' % location)
            return
        output = check_output('rm "{0}"'.format(location), shell=True).strip()
        output = output.replace('\xe2\x80\x98', '"').replace('\xe2\x80\x99', '"')
        logger.info(output)
        if os.path.exists(location):
            raise RuntimeError('Could not delete file %s, check logs. Output: %s' % (location, output))
        if output == '':
            return True
        raise RuntimeError(output)

    @staticmethod
    @celery.task(name='ovs.vdisk.extend_volume')
    def extend_volume(location, size):
        """
        Extend a volume using filesystem calls
        Calls "truncate" to create sparse raw file
        TODO: use volumedriver API
        TODO: model VDisk() and return guid

        @param location: location, filename
        @param size: size of volume, GB
        @return None
        """
        if not os.path.exists(location):
            raise RuntimeError('Volume not found at %s, use create_volume first.' % location)
        output = check_output('truncate -s {0}G "{1}"'.format(size, location), shell=True).strip()
        output = output.replace('\xe2\x80\x98', '"').replace('\xe2\x80\x99', '"')
        logger.info(output)

    @staticmethod
    @celery.task(name='ovs.vdisk.update_vdisk_name')
    def update_vdisk_name(volume_id, old_name, new_name):
        """
        Update a vDisk name using Management Center: set new name
        """
        vdisk = None
        for mgmt_center in MgmtCenterList.get_mgmtcenters():
            mgmt = Factory.get_mgmtcenter(mgmt_center = mgmt_center)
            try:
                disk_info = mgmt.get_vdisk_device_info(volume_id)
                device_path = disk_info['device_path']
                vpool_name = disk_info['vpool_name']
                vp = VPoolList.get_vpool_by_name(vpool_name)
                file_name = os.path.basename(device_path)
                vdisk = VDiskList.get_by_devicename_and_vpool(file_name, vp)
                if vdisk:
                    break
            except Exception as ex:
                logger.info('Trying to get mgmt center failed for disk {0} with volume_id {1}. {2}'.format(old_name, volume_id, ex))
        if not vdisk:
            logger.error('No vdisk found for name {0}'.format(old_name))
            return

        vpool = vdisk.vpool
        mutex = VolatileMutex('{}_{}'.format(old_name, vpool.guid if vpool is not None else 'none'))
        try:
            mutex.acquire(wait=5)
            vdisk.name = new_name
            vdisk.save()
        finally:
            mutex.release()

    @staticmethod
    def sync_with_mgmtcenter(disk, pmachine, storagedriver):
        """
        Update disk info using management center (if available)
         If no management center, try with hypervisor
         If no info retrieved, use devicename
        @param disk: vDisk hybrid (vdisk to be updated)
        @param pmachine: pmachine hybrid (pmachine running the storagedriver)
        @param storagedriver: storagedriver hybrid (storagedriver serving the vdisk)
        """
        disk_name = None
        if pmachine.mgmtcenter is not None:
            logger.debug('Sync vdisk {0} with management center {1} on storagedriver {2}'.format(disk.name, pmachine.mgmtcenter.name, storagedriver.name))
            mgmt = Factory.get_mgmtcenter(mgmt_center = pmachine.mgmtcenter)
            volumepath = disk.devicename
            mountpoint = storagedriver.mountpoint
            devicepath = '{0}/{1}'.format(mountpoint, volumepath)
            try:
                disk_mgmt_center_info = mgmt.get_vdisk_model_by_devicepath(devicepath)
                if disk_mgmt_center_info is not None:
                    disk_name = disk_mgmt_center_info.get('name')
            except Exception as ex:
                logger.error('Failed to sync vdisk {0} with mgmt center {1}. {2}'.format(disk.name, pmachine.mgmtcenter.name, str(ex)))

        if disk_name is None:
            logger.info('Sync vdisk with hypervisor on {0}'.format(pmachine.name))
            try:
                hv = Factory.get(pmachine)
                info = hv.get_vm_agnostic_object(disk.vmachine.hypervisor_id)
                for disk in info['disks']:
                    if disk['filename'] == disk.devicename:
                        disk_name = disk['name']
                        break
            except Exception as ex:
                logger.error('Failed to get vdisk info from hypervisor. %s' % ex)

        if disk_name is None:
            logger.info('No info retrieved from hypervisor, using devicename')
            disk_name = disk.devicename.split('/')[-1].split('.')[0]

        if disk_name is not None:
            disk.name = disk_name
            disk.save()
