#!/usr/bin/env python3

"""
 ****************************************************************************
 Filename:          usl.py
 Description:       Services for USL calls

 Creation Date:     10/21/2019
 Author:            Alexander Voronov

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2015/01/14 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""

from aiohttp import web, ClientSession
from botocore.exceptions import ClientError
from datetime import date
from random import SystemRandom
from marshmallow import ValidationError
from marshmallow.validate import URL
from typing import Any, Dict, List
from uuid import UUID, uuid4, uuid5
import asyncio
import time
import toml

from csm.common.conf import Conf
from csm.common.errors import CsmInternalError, CsmNotFoundError
from csm.common.log import Log
from csm.common.services import ApplicationService
from csm.core.blogic import const
from eos.utils.data.access import Query
from eos.utils.data.access.filters import Compare
from eos.utils.data.db.db_provider import DataBaseProvider
from csm.core.data.models.s3 import S3ConnectionConfig, IamUser
from csm.core.data.models.usl import (Device, Volume, NewVolumeEvent, VolumeRemovedEvent,
                                      MountResponse)
from csm.core.services.s3.utils import CsmS3ConfigurationFactory, IamRootClient
from csm.core.services.usl_certificate_manager import (
    USLDomainCertificateManager, USLNativeCertificateManager, CertificateError
)

DEFAULT_EOS_DEVICE_VENDOR = 'Seagate'
DEFAULT_VOLUME_CACHE_UPDATE_PERIOD = 3


class UslService(ApplicationService):
    """
    Implements USL service operations.
    """
    # FIXME improve token management
    _token: str
    _s3plugin: Any
    _s3cli: Any
    _iamcli: Any
    _storage: DataBaseProvider
    _device: Device
    _volumes: Dict[UUID, Volume]
    _volumes_sustaining_task: asyncio.Task
    _event_queue: asyncio.Queue
    _domain_certificate_manager: USLDomainCertificateManager
    _native_certificate_manager: USLNativeCertificateManager

    def __init__(self, s3_plugin, storage) -> None:
        """
        Constructor.
        """
        loop = asyncio.get_event_loop()

        self._token = ''
        self._s3plugin = s3_plugin
        self._s3cli = self._create_s3cli(s3_plugin)
        self._iamcli = IamRootClient()
        dev_uuid = self._get_device_uuid()
        self._device = Device.instantiate(
            self._get_system_friendly_name(),
            '0000',
            str(dev_uuid),
            'S3',
            dev_uuid,
            DEFAULT_EOS_DEVICE_VENDOR,
        )
        self._storage = storage
        self._event_queue = asyncio.Queue(0, loop=loop)
        self._volumes = loop.run_until_complete(self._restore_volume_cache())
        self._volumes_sustaining_task = loop.create_task(self._sustain_cache())
        self._domain_certificate_manager = USLDomainCertificateManager()
        self._native_certificate_manager = USLNativeCertificateManager()

    # TODO: pass S3 server credentials to the server instead of reading from a file
    def _create_s3cli(self, s3_plugin):
        """Creates the S3 client for USL service"""

        s3_conf = S3ConnectionConfig()
        s3_conf.host = Conf.get(const.CSM_GLOBAL_INDEX, 'S3.host')
        s3_conf.port = Conf.get(const.CSM_GLOBAL_INDEX, 'S3.s3_port')

        usl_s3_conf = toml.load(const.USL_S3_CONF)
        return s3_plugin.get_s3_client(usl_s3_conf['credentials']['access_key_id'],
                                       usl_s3_conf['credentials']['secret_key'],
                                       s3_conf)

    async def _restore_volume_cache(self) -> Dict[UUID, Volume]:
        """Restores the volume cache from Consul KVS"""

        try:
            cache = {volume.uuid: volume
                     for volume in await self._storage(Volume).get(Query())}
        except Exception as e:
            reason = (f"Failed to restore USL volume cache from Consul KVS: {str(e)}\n"
                      f"All volumes are considered new. Redundant events may appear")
            Log.error(reason)
            cache = {}
        return cache

    def _get_system_friendly_name(self) -> str:
        return str(Conf.get(const.CSM_GLOBAL_INDEX, 'PRODUCT.friendly_name') or 'local')

    def _get_device_uuid(self) -> UUID:
        """Obtains the EOS device UUID from config."""

        return UUID(Conf.get(const.CSM_GLOBAL_INDEX, "PRODUCT.uuid")) or uuid4()

    def _get_volume_name(self, bucket_name: str) -> UUID:
        return self._get_system_friendly_name() + ": " + bucket_name

    def _get_volume_uuid(self, bucket_name: str) -> UUID:
        """Generates the EOS volume (bucket) UUID from EOS device UUID and bucket name."""

        return uuid5(self._device.uuid, bucket_name)

    async def _handle_udx_s3_registration(self, s3_session, iam_user_name: str, iam_user_passwd: str,
                                          bucket_name: str) -> Dict:
        """
        Handles an S3 part of UDX device registration:
        - creates UDX IAM account inside the currently logged in S3 account
        - creates UDX bucket inside the currently logged in S3 account
        - tags UDX bucket with {"udx": "enabled"}
        - grants UDX IAM user full access to the UDX bucket
        In case of error on any of the steps above, attempts to perform a cleanup, i.e.:
        - delete UDX bucket (if it hadn't existed before the registration)
        - delete UDX IAM account (if it hadn't existed before the registration)
        """

        iam_conn_conf = CsmS3ConfigurationFactory.get_iam_connection_config()
        iam_cli = self._s3plugin.get_iam_client(s3_session.access_key, s3_session.secret_key,
                                                iam_conn_conf, s3_session.session_token)

        s3_conn_conf = CsmS3ConfigurationFactory.get_s3_connection_config()
        s3_cli = self._s3plugin.get_s3_client(s3_session.access_key, s3_session.secret_key,
                                              s3_conn_conf)

        iam_user = None
        bucket = None
        try:
            iam_user = await self._get_udx_iam_user(iam_cli, iam_user_name)
            iam_user_already_exists = iam_user is not None
            if not iam_user_already_exists:
                iam_user = await self._create_udx_iam_user(iam_cli, iam_user_name, iam_user_passwd)
            iam_user_credentials = await self._get_udx_iam_user_credentials(iam_cli, iam_user_name)
            bucket = await self._get_udx_bucket(s3_cli, bucket_name)
            bucket_already_exists = bucket is not None
            if not bucket_already_exists:
                bucket = await self._create_udx_bucket(s3_cli, bucket_name)
            udx_bucket_name = bucket.name
            await self._tag_udx_bucket(s3_cli, udx_bucket_name)
            await self._set_udx_policy(s3_cli, iam_user, udx_bucket_name)
        except (CsmInternalError, ClientError) as e:
            erorr_msg = f"Failed to accomplish UDX S3 registration: {str(e)}"
            # Don't delete UDX bucket that had existed before the registration
            if bucket is not None and not bucket_already_exists:
                await s3_cli.delete_bucket(udx_bucket_name)
                Log.info(f'UDX Bucket {udx_bucket_name} is removed '
                         f'during failed registration clean-up')
            # Don't delete IAM user that had existed before the registration
            if iam_user is not None and not iam_user_already_exists:
                await iam_cli.delete_user(iam_user.user_name)
                Log.info(f'UDX IAM user {iam_user_name} is removed '
                         f'during failed registration clean-up')
            raise CsmInternalError(erorr_msg)

        return {
            'iam_user_name': iam_user.user_name,
            'access_key_id': iam_user_credentials['access_key_id'],
            'secret_key': iam_user_credentials['secret_key'],
            'bucket_name': bucket.name,
        }

    async def _get_udx_iam_user(self, iam_cli, user_name: str) -> IamUser:
        """
        Checks UDX IAM user exists and returns it
        """

        # TODO: Currently the IAM server does not support 'get-user' operation.
        # Thus there is no way to obtain details about existing IAM user: ID and ARN.
        # Workaround: delete IAM user even if it exists (and recreate then).
        # When get-user is implemented on the IAM server side workaround could be removed.
        try:
            await iam_cli.delete_user(user_name)
        except ClientError:
            # Ignore errors in deletion, user might not exist
            pass

        return None

    async def _create_udx_iam_user(self, iam_cli, user_name: str, user_passwd: str) -> IamUser:
        """
        Creates UDX IAM user inside the currently logged in S3 account
        """

        Log.debug(f'Creating UDX IAM user {user_name}')
        iam_user_resp = await iam_cli.create_user(user_name)
        if hasattr(iam_user_resp, "error_code"):
            erorr_msg = iam_user_resp.error_message
            raise CsmInternalError(f'Failed to create UDX IAM user: {erorr_msg}')
        Log.info(f'UDX IAM user {user_name} is created')

        iam_login_resp = await iam_cli.create_user_login_profile(user_name, user_passwd, False)
        if hasattr(iam_login_resp, "error_code"):
            # Remove the user if the login profile creation failed
            await iam_cli.delete_user(user_name)
            error_msg = iam_login_resp.error_message
            raise CsmInternalError(f'Failed to create login profile for UDX IAM user {error_msg}')
        Log.info(f'Login profile for UDX IAM user {user_name} is created')

        return iam_user_resp

    async def _get_udx_iam_user_credentials(self, iam_cli, user_name: str) -> Dict:
        """
        Gets the access key id and secret key for UDX IAM user
        """

        # TODO: this is a STUB! IAM user key creation/listing/deletion is not implemented yet
        # and TBD in EES sprint 17.
        # Replace with the actual IAM client call when ready
        return {
            'access_key_id': '',
            'secret_key': '',
        }

    def _get_udx_bucket_name(self, bucket_name: str) -> str:
        return 'udx-' + bucket_name

    async def _get_udx_bucket(self, s3_cli, bucket_name: str):
        """
        Checks if UDX bucket already exists and returns it
        """

        Log.debug(f'Getting UDX bucket')
        udx_bucket_name = self._get_udx_bucket_name(bucket_name)
        bucket = await s3_cli.get_bucket(udx_bucket_name)

        return bucket

    async def _create_udx_bucket(self, s3_cli, bucket_name: str):
        """
        Creates UDX bucket inside the curretnly logged in S3 account
        """

        Log.debug(f'Creating UDX bucket {bucket_name}')
        udx_bucket_name = self._get_udx_bucket_name(bucket_name)
        bucket = await s3_cli.create_bucket(udx_bucket_name)
        Log.info(f'UDX bucket {udx_bucket_name} is created')
        return bucket

    async def _tag_udx_bucket(self, s3_cli, bucket_name: str):
        """
        Puts the UDX tag on a specified bucket
        """

        Log.debug(f'Tagging bucket {bucket_name} with UDX tag')
        bucket_tags = {"udx": "enabled"}
        await s3_cli.put_bucket_tagging(bucket_name, bucket_tags)
        Log.info(f'UDX bucket {bucket_name} is taggged with {bucket_tags}')

    async def _set_udx_policy(self, s3_cli, iam_user, bucket_name: str):
        """
        Grants the specified IAM user full access to the specified bucket
        """

        Log.debug(f'Setting UDX policy for bucket {bucket_name} and IAM user {iam_user.user_name}')
        policy = {
            'Version': str(date.today()),
            'Statement': [{
                'Sid': 'UdxIamAccountPerm',
                'Effect': 'Allow',
                'Principal': {"AWS": iam_user.arn},
                'Action': ['s3:GetObject', 's3:PutObject',
                           's3:ListMultipartUploadParts', 's3:AbortMultipartUpload',
                           's3:GetObjectAcl', 's3:PutObjectAcl',
                           's3:PutObjectTagging',
                           # TODO: now S3 server rejects the following policies
                           # 's3:DeleteObject', 's3:RestoreObject', 's3:DeleteObjectTagging',
                           ],
                'Resource': f'arn:aws:s3:::{bucket_name}/*',
            }]
        }
        await s3_cli.put_bucket_policy(bucket_name, policy)
        Log.info(f'UDX policy is set for bucket {bucket_name} and IAM user {iam_user.user_name}')

    async def _is_bucket_udx_enabled(self, bucket):
        """
        Checks if bucket is UDX enabled

        The UDX enabled bucket contains tag {Key=udx,Value=enabled}
        """

        tags = await self._s3cli.get_bucket_tagging(bucket)
        return tags.get('udx', 'disabled') == 'enabled'

    async def _sustain_cache(self):
        """The infinite asynchronous task that sustains volumes cache"""

        volume_cache_update_period = float(
            Conf.get(const.CSM_GLOBAL_INDEX, 'UDS.volume_cache_update_period_seconds') or
            DEFAULT_VOLUME_CACHE_UPDATE_PERIOD
        )

        while True:
            await asyncio.sleep(volume_cache_update_period)
            try:
                await self._update_volumes_cache()
            except asyncio.CancelledError:
                break
            except Exception as e:
                reason = "Unpredictable exception during volume cache update" + str(e)
                # Do not fail here, keep trying to update the cache
                Log.error(reason)

    async def _get_volume_cache(self) -> Dict[UUID, Volume]:
        """
        Creates the internal volumes cache from buckets list retrieved from S3 server
        """
        volumes = {}
        for b in await self._s3cli.get_all_buckets():
            if await self._is_bucket_udx_enabled(b):
                volume_uuid = self._get_volume_uuid(b.name)
                volumes[volume_uuid] = Volume.instantiate(self._get_volume_name(b.name), b.name,
                                                          self._device.uuid, volume_uuid)
        return volumes

    async def _update_volumes_cache(self):
        """
        Updates the internal buckets cache.

        Obtains the fresh buckets list from S3 server and updates cache with it.
        Keeps cache the same if the server is not available.
        """
        fresh_cache = await self._get_volume_cache()

        new_volume_uuids = fresh_cache.keys() - self._volumes.keys()
        volume_removed_uuids = self._volumes.keys() - fresh_cache.keys()

        for uuid in new_volume_uuids:
            e = NewVolumeEvent.instantiate(fresh_cache[uuid])
            await self._event_queue.put(e)

        for uuid in volume_removed_uuids:
            e = VolumeRemovedEvent.instantiate(uuid)
            await self._event_queue.put(e)

        self._volumes = fresh_cache

    async def get_device_list(self) -> List[Dict[str, str]]:
        """
        Provides a list with all available devices.

        :return: A list with dictionaries, each containing information about a specific device.
        """

        return [self._device.to_primitive()]

    async def get_device_volumes_list(self, device_id: UUID) -> List[Dict[str, Any]]:
        """
        Provides a list of all volumes associated to a specific device.

        :param device_id: Device UUID
        :return: A list with dictionaries, each containing information about a specific volume.
        """

        if device_id != self._device.uuid:
            raise CsmNotFoundError(desc=f'Device with ID {device_id} is not found')
        return [v.to_primitive(role='public') for uuid, v in self._volumes.items()]

    async def post_device_volume_mount(self, device_id: UUID, volume_id: UUID) -> Dict[str, str]:
        """
        Attaches a volume associated to a specific device to a mount point.

        :param device_id: Device UUID
        :param volume_id: Volume UUID
        :return: A dictionary containing the mount handle and the mount path.
        """
        if device_id != self._device.uuid:
            raise CsmNotFoundError(desc=f'Device with ID {device_id} is not found')

        if volume_id not in self._volumes:
            raise CsmNotFoundError(desc=f'Volume {volume_id} is not found')
        return MountResponse.instantiate(self._volumes[volume_id].bucketName,
                                         self._volumes[volume_id].bucketName).to_primitive()

    # TODO replace stub
    async def post_device_volume_unmount(self, device_id: UUID, volume_id: UUID) -> str:
        """
        Detaches a volume associated to a specific device from its current mount point.

        The current implementation reflects the API specification but does nothing.

        :param device_id: Device UUID
        :param volume_id: Volume UUID
        :return: The volume's mount handle
        """
        return 'handle'

    async def get_events(self) -> str:
        """
        Returns USL events one-by-one
        """
        e = await self._event_queue.get()
        if isinstance(e, NewVolumeEvent):
            await self._storage(Volume).store(e.volume)
        elif isinstance(e, VolumeRemovedEvent):
            await self._storage(Volume).delete(Compare(Volume.uuid, '=', e.uuid))
        else:
            raise CsmInternalError("Unknown entry in USL events queue")
        return e.to_primitive(role='public')

    async def register_device(self, url: str, pin: str) -> None:
        """
        Executes device registration sequence. Communicates with the UDS server in order to start
        registration and verify its status.

        :param url: Registration URL as provided by the UDX portal
        :param pin: Registration PIN as provided by the UDX portal
        """
        uds_url = Conf.get(const.CSM_GLOBAL_INDEX, 'UDS.url') or const.UDS_SERVER_DEFAULT_BASE_URL
        try:
            validate_url = URL(schemes=('http', 'https'))
            validate_url(uds_url)
        except ValidationError:
            reason = 'UDS base URL is not valid'
            Log.error(reason)
            raise web.HTTPInternalServerError(reason=reason)
        endpoint_url = str(uds_url) + '/uds/v1/registration/RegisterDevice'
        # TODO use a single client session object; manage life cycle correctly
        async with ClientSession() as session:
            params = {'url': url, 'regPin': pin, 'regToken': self._token}
            Log.info(f'Start device registration at {uds_url}')
            async with session.put(endpoint_url, params=params) as response:
                if response.status != 201:
                    reason = 'Could not start device registration'
                    Log.error(f'{reason}---unexpected status code {response.status}')
                    raise web.HTTPInternalServerError(reason=reason)
            Log.info('Device registration in process---waiting for confirmation')
            timeout_limit = time.time() + 60
            while time.time() < timeout_limit:
                async with session.get(endpoint_url) as response:
                    if response.status == 200:
                        Log.info('Device registration successful')
                        return
                    elif response.status != 201:
                        reason = 'Device registration failed'
                        Log.error(f'{reason}---unexpected status code {response.status}')
                        raise web.HTTPInternalServerError(reason=reason)
                await asyncio.sleep(1)
            else:
                reason = 'Could not confirm device registration status'
                Log.error(reason)
                raise web.HTTPGatewayTimeout(reason=reason)

    # TODO replace stub
    async def get_registration_token(self) -> Dict[str, str]:
        """
        Generates a random registration token.

        :return: A 12-digit token.
        """
        self._token = ''.join(SystemRandom().sample('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', 12))
        return {'registrationToken': self._token}

    # TODO replace stub
    async def get_system(self) -> Dict[str, str]:
        """
        Provides information about the system.

        :return: A dictionary containing system information.
        """
        friendly_name = self._get_system_friendly_name()
        return {
            'model': 'EES',
            'type': 'ees',
            'serialNumber': self._device.uuid,
            'friendlyName': friendly_name,
            'firmwareVersion': '0.00',
        }

    async def post_system_certificates(self) -> web.Response:
        """
        Create USL domain key pair in case it does not exist.

        :returns: USL public key as an ``application/octet-stream`` HTTP response
        """
        if await self._domain_certificate_manager.get_private_key_bytes() is not None:
            raise web.HTTPForbidden()
        await self._domain_certificate_manager.create_private_key_file(overwrite=False)
        private_key_bytes = await self._domain_certificate_manager.get_private_key_bytes()
        if private_key_bytes is None:
            reason = 'Could not read USL private key'
            Log.error(reason)
            raise web.HTTPInternalServerError(reason=reason)
        body = await self._domain_certificate_manager.get_public_key_bytes()
        if body is None:
            reason = 'Could not read USL public key'
            Log.error(f'{reason}')
            raise web.HTTPInternalServerError(reason=reason)
        return web.Response(body=body)

    async def put_system_certificates(self, certificate: bytes) -> None:
        if await self._domain_certificate_manager.get_certificate_bytes() is not None:
            raise web.HTTPForbidden()
        try:
            await self._domain_certificate_manager.create_certificate_file(certificate)
        except CertificateError as e:
            reason = 'Could not update USL certificate'
            Log.error(f'{reason}: {e}')
            raise web.HTTPInternalServerError(reason=reason)
        raise web.HTTPNoContent()

    async def delete_system_certificates(self) -> None:
        """
        Delete all key material related with the USL domain certificate.
        """
        try:
            await self._domain_certificate_manager.delete_key_material()
        except FileNotFoundError:
            raise web.HTTPForbidden() from None
        # Don't return 200 on success, but 204 as USL API specification requires
        raise web.HTTPNoContent()

    async def get_system_certificates_by_type(self, material_type: str) -> web.Response:
        """
        Provides key material according to the specified type.

        :param material_type: Key material type
        :return: The corresponding key material as an ``application/octet-stream`` HTTP response
        """
        get_material_bytes = {
            'domainCertificate': self._domain_certificate_manager.get_certificate_bytes,
            'domainPrivateKey': self._domain_certificate_manager.get_private_key_bytes,
            'nativeCertificate': self._native_certificate_manager.get_certificate_bytes,
            'nativePrivateKey': self._native_certificate_manager.get_private_key_bytes,
        }.get(material_type)
        if get_material_bytes is None:
            reason = f'Unexpected key material type "{material_type}"'
            Log.error(reason)
            raise web.HTTPInternalServerError(reason=reason)
        body = await get_material_bytes()
        if body is None:
            raise web.HTTPNotFound()
        return web.Response(body=body)

    # TODO replace stub
    async def get_network_interfaces(self) -> List[Dict[str, Any]]:
        """
        Provides a list of all network interfaces in a system.

        :return: A list containing dictionaries, each containing information about a specific
            network interface.
        """
        return [
            {
                'name': 'tbd',
                'type': 'tbd',
                'macAddress': 'AA:BB:CC:DD:EE:FF',
                'isActive': True,
                'isLoopback': False,
                'ipv4': '127.0.0.1',
                'netmask': '255.0.0.0',
                'broadcast': '127.255.255.255',
                'gateway': '127.255.255.254',
                'ipv6': '::1',
                'link': 'tbd',
                'duplex': 'tbd',
                'speed': 0,
            }
        ]
