import os
import requests
import json
import logging
import threading
from pathlib import Path

from astropy.utils.data import download_file
from astropy.utils.console import ProgressBarOrSpinner

import boto3
import botocore


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
log = logging.getLogger('fornax')


__all__ = ['get_data_product', 'DataHandler', 'AWSDataHandler', 'AWSDataHandlerError']



def get_data_product(product, provider='on-prem', access_url_column='access_url',
                     access_summary_only=False, verbose=True, **kwargs):
    """Top layer function to handle cloud/non-cloud access

    Parameters
    ----------
    product : astropy.table.Row
        The data url is accessed in product[access_url_column]
    provider : str
        Data provider to use. Options are: on-prem | aws
    access_url_column : str
        The name of the column that contains the access url.
        This is typically the column with ucd 'VOX:Image_AccessReference'
        in SIA.

    kwargs: other arguments to be passed to DataHandler or its subclasses
    """

    handler = None
    if provider == 'on-prem':
        handler = DataHandler(product, access_url_column)
    elif provider == 'aws':
        handler = AWSDataHandler(product, access_url_column, **kwargs)
    else:
        raise Exception(f'Unable to handle provider {provider}')

    if verbose:
        handler._summary()

    if not access_summary_only:
        return handler


class DataHandler:
    """A base class that handles the different ways data can be accessed.
    The base implementation is to use on-prem data. Subclasses can handle
    cloud data from aws, azure, google cloud etc.

    Subclasses can also handle authentication if needed.

    """

    def __init__(self, product, access_url_column='access_url'):
        """Create a DataProvider object.

        Parameters
        ----------
        product : astropy.table.Row
            The data url is accessed in product[access_url_column]
        access_url_column : str
            The name of the column that contains the access url.
            This is typically the column with ucd 'VOX:Image_AccessReference'
            in SIA.

        """
        self.product = product
        self.access_url = product[access_url_column]
        self.processed_info = None

    def process_data_info(self):
        """Process data product info """
        if self.processed_info is None:
            info = {'access_url': self.access_url}
            info['message'] = 'Accessing data from on-prem servers.'
            self.processed_info = info
        log.info(self.processed_info['message'])
        return self.processed_info

    def _summary(self):
        """A str representation of the data handler"""
        info = self.process_data_info()
        print('\n---- Summary ----')
        for key, value in info.items():
            print(f'{key:12}: {value}')
        print('-----------------\n')

    def download(self):
        """Download data. Can be overloaded with different implimentation"""
        info = self.process_data_info()
        return download_file(info['access_url'])


class AWSDataHandlerError(Exception):
    pass


class AWSDataHandler(DataHandler):
    """Class for managaing access to data in AWS"""

    def __init__(self, product, access_url_column='access_url', profile=None, requester_pays=False):
        """Handle AWS-specific authentication and data download

        Requires boto3

        Parameters
        ----------
        product : astropy.table.Row.
            aws-s3 information should be available in product['cloud_access'],
            otherwise, fall back to on-prem using product[access_url_column]
        access_url_column : str
            The name of the column that contains the access url for on-prem
            access fall back. This is typically the column with ucd
            'VOX:Image_AccessReference' in SIA.
        profile : str
            name of the user's profile for credentials in ~/.aws/config
            or ~/.aws/credentials. Use to authenticate the AWS user with
            boto3 when needed.
        requester_pays : bool
            Used when the data has region-restricted access and the user
            want to explicitely pay for cross region access. Requires aws
            authentication using a profile.

        """

        if requester_pays:
            raise NotImplementedError('requester_pays is not implemented.')

        super().__init__(product, access_url_column)

        # set variables to be used elsewhere
        self.requester_pays = requester_pays
        self.profile = profile
        self.product = product
        self._s3_uri = None
        self.bucket_access = {}

    @property
    def s3_uri(self):
        if not self._s3_uri:
            self.process_data_info()
            self._s3_uri = f's3://{self.processed_info["s3_bucket_name"]}/{self.processed_info["s3_key"]}'
        return self._s3_uri

    def _validate_aws_info(self, info):
        """Do some basic validation of the json information provided in the
        data product's cloud_access column.

        Parameters
        ----------
        info : dict
            A dictionary serialized from the json text returned in the cloud_access
            column returned with the data product

        Returns
        -------
        The same input dict with any standardization applied.
        """

        # TODO; more rigorous checks
        keys = list(info.keys())
        assert('region' in keys)
        assert('access' in keys)
        assert(info['access'] in ['none', 'open', 'restricted', 'region'])
        assert('bucket_name' in keys)
        assert('key' in keys)

        if info['key'][0] == '/':
            info['key'] = info['key'][1:]

        return info

    def _process_single_aws_entry(self, aws_info):
        """Extract the AWS access information from the metadata provided by the server

        Parameters
        ----------
        aws_info: dict
            A dictionay serialized from the json text returned in the cloud_access column
            returned with the data product.

        Returns
        -------
        A dict containing the information needed by boto3 to access the data in s3.
            s3_path, s3_bucket, s3_resource, data_region and data_access (for data mode).
            The dict also contains a message as a str to describe the result of the attempted
            access.

        Raises
        ------
        AWSDataHandlerError if the data cannot be accessed for some reason.

        """
        # data holder
        aws_access_info = {}

        # first, validate the information provided with the data product
        try:
            aws_info = self._validate_aws_info(aws_info)
        except Exception as e:
            raise AWSDataHandlerError(str(e))

        # region and access mode
        data_region = aws_info['region']
        data_access = aws_info['access']  # open | region | restricted | none
        data_bucket = aws_info['bucket_name']
        data_path = aws_info['key']

        log.info(f'data region: {data_region}')
        log.info(f'data access mode: {data_access}')

        # check if we already have access to data_bucket
        if data_bucket in self.bucket_access.keys():
            aws_access_info = self.bucket_access[data_bucket]
            aws_access_info['s3_path'] = data_path
            aws_access_info['message'] += f', re-using access.'
            aws_access_info['data_region'] = data_region
            aws_access_info['data_access'] = data_access
            log.info(f'Reusing access information for {data_bucket}')
            return aws_access_info


        # data on aws not accessible for some reason
        if data_access == 'none':
            msg = 'Data access mode is "none".'
            raise AWSDataHandlerError(msg)

        # data have open access
        if data_access == 'open':
            s3_config = botocore.client.Config(signature_version=botocore.UNSIGNED)
            s3_resource = boto3.resource(service_name='s3', config=s3_config)
            accessible, message = self.is_accessible(s3_resource, data_bucket, data_path)
            msg = 'Accessing public data anonymously on aws ... '
            if not accessible:
                msg = f'{msg}  {message}'
                raise AWSDataHandlerError(msg)

        # data is restricted by region.
        elif data_access in ['region', 'restricted']:

            accessible = False
            messages = []
            while not accessible:

                # -----------------------
                # NOTE: THIS MAY NEED TO BE COMMENTED OUT BECAUSE IT MAY NOT BE POSSIBLE
                # TO ACCESS REGION-RESTRICTED DATA ANONYMOUSLY.
                # -----------------------
                # Attempting anonymous access:

                if data_access == 'region':
                    msg = f'Accessing {data_access} data anonymously ...'
                    s3_config = botocore.client.Config(signature_version=botocore.UNSIGNED)
                    s3_resource = boto3.resource(service_name='s3', config=s3_config)
                    accessible, message = self.is_accessible(s3_resource, data_bucket, data_path)
                    if accessible:
                        break
                    message = f'  - {msg} {message}.'
                    messages.append(message)

                # If profile is given, try to use it first as it takes precedence.
                if self.profile is not None:
                    msg = f'Accessing {data_access} data using profile: {self.profile} ...'
                    try:
                        s3_session = boto3.session.Session(profile_name=self.profile)
                        s3_resource = s3_session.resource(service_name='s3')
                        accessible, message = self.is_accessible(s3_resource, data_bucket, data_path)
                        if accessible:
                            break
                        else:
                            raise AWSDataHandlerError(message)
                    except Exception as e:
                        message = f'  - {msg} {str(e)}.'
                    messages.append(message)

                # If access with profile fails, attemp to use any credientials
                # in the user system e.g. environment variables etc. boto3 should find them.
                msg = f'Accessing {data_access} data with other credentials ...'
                s3_resource = boto3.resource(service_name='s3')
                accessible, message = self.is_accessible(s3_resource, data_bucket, data_path)
                if accessible:
                    break
                message = f'  - {msg} {message}.'
                messages.append(message)

                # if we are here, then we cannot access the data. Fall back to on-prem
                msg = f'\nUnable to authenticate or access data with "{data_access}" access mode:\n'
                msg += '\n'.join(messages)
                raise AWSDataHandlerError(msg)
        else:
            msg = f'Unknown data access mode: {data_access}.'
            raise AWSDataHandlerError(msg)

        # if we make it here, we have valid aws access information.
        aws_access_info['s3_key'] = data_path
        aws_access_info['s3_bucket_name'] = data_bucket
        aws_access_info['message'] = msg
        aws_access_info['s3_resource'] = s3_resource
        aws_access_info['data_region'] = data_region
        aws_access_info['data_access'] = data_access

        # save some info in case we need it later
        self.bucket_access[data_bucket] = aws_access_info

        return aws_access_info

    def process_data_info(self):
        """Process cloud infromation from data product metadata

        This returns a dict which contains information on how to access
        the data. The main logic that attempts to interpret the cloud_access
        column provided for the data product. If sucessfully processed, the
        access details (bucket_name, key etc) are added to the final dictionary.
        If any information is missing, the returned dict will return access_url
        that allows points to the data location in the on-prem servers as
        a backup.

        """

        if self.processed_info is not None:
            return self.processed_info

        # Get a default on-prem-related information
        info = {
            'access_url': self.access_url,
            'message': 'Accessing data from on-prem servers.'
        }

        try:
            # do we have cloud_access info in the data product?
            if 'cloud_access' not in self.product.keys():
                msg = 'Input product does not have any cloud access information.'
                raise AWSDataHandlerError(msg)

            # read json provided by the archive server
            cloud_access = json.loads(self.product['cloud_access'])

            # do we have information specific to aws in the data product?
            if 'aws' not in cloud_access:
                msg = 'No aws cloud access information in the data product.'
                raise AWSDataHandlerError(msg)

            # we have info about data in aws; validate it first #
            # TODO: add support for multiple aws access points. This may be useful
            aws_info = cloud_access['aws']

            if isinstance(aws_info, list) and len(aws_info) == 1:
                aws_info = aws_info[0]

            # we have a single aws access point given as a dict
            if isinstance(aws_info, dict):
                aws_access_info = self._process_single_aws_entry(aws_info)
                info.update(aws_access_info)

            # we have multiple aws access points given as a list of dict
            elif isinstance(aws_info, list):
                aws_access_info = [self._process_single_aws_entry(aws_i) for aws_i in aws_info]

                # sort access points so that open data comes first
                sorter = {'open': 0, 'region': 1, 'restricted': 2, 'none': 3}
                aws_access_info = sorted(aws_access_info, key=lambda x: sorter[x['data_access']])
                aws_access_info = aws_access_info[0]
                info.update(aws_access_info)
            else:
                msg = f'Unrecognized aws entry: {type(access_info)}. Expected a dict or a list'
                raise AWSDataHandlerError(msg)

        except AWSDataHandlerError as e:
            info['message'] += str(e)

        self.processed_info = info
        return self.processed_info

    def is_accessible(self, s3_resource, bucket_name, key):
        """Do a head_object call to test access

        Paramters
        ---------
        s3_resource : s3.ServiceResource
            the service resource used for s3 connection.
        bucket_name : str
            bucket name.
        key : str
            key to file to test.

        Return
        -----
        (accessible, msg) where accessible is a bool and msg is the failure message

        """

        s3_client = s3_resource.meta.client

        try:
            header_info = s3_client.head_object(Bucket=bucket_name, Key=key)
            accessible, msg = True, ''
        except Exception as e:
            accessible = False
            msg = str(e)

        return accessible, msg

    def download(self, **kwargs):
        """Download data, from aws if possible, else from on-prem"""

        data_info = self.process_data_info()

        # if no s3_resource object, default to http download
        if 's3_resource' in data_info.keys():
            log.info('--- Downloading data from S3 ---')
            self._download_file_s3(data_info, **kwargs)
        else:
            log.info('--- Downloading data from On-prem ---')
            download_file(data_info['access_url'])

    def length_file_s3(self):
        """
        Gets info from s3 and prints the file length or exception.
        """
        data_info = self.process_data_info()

        s3 = data_info.get('s3_resource', None)
        if not s3:
            print('Checking length: No AWS info available')
            return

        s3_client = s3.meta.client

        key = data_info['s3_key']
        bucket_name = data_info['s3_bucket_name']
        bkt = s3.Bucket(bucket_name)
        if not key:
            raise Exception(f"Unable to locate file {key}.")

        # Ask the webserver (in this case S3) what the expected content length is.
        ex = ''
        try:
            info_lookup = s3_client.head_object(Bucket=bucket_name, Key=key)
            length = info_lookup["ContentLength"]
        except Exception as e:
            ex = e
            length = 0

        print(f'Checking length: {key=}, {ex=}, {length=}')

    # adapted from astroquery.mast.
    def _download_file_s3(self, data_info, local_path=None, cache=True):
        """
        downloads the product used in inializing this object into
        the given directory.
        Parameters
        ----------
        data_info : dict holding the data information, with keys for:
            s3_resource, s3_key, s3_bucket_name
        local_path : str
            The local filename to which toe downloaded file will be saved.
        cache : bool
            Default is True. If file is found on disc it will not be downloaded again.
        """

        s3 = data_info['s3_resource']
        s3_client = s3.meta.client

        key = data_info['s3_key']
        bucket_name = data_info['s3_bucket_name']
        bkt = s3.Bucket(bucket_name)
        if not key:
            raise Exception(f"Unable to locate file {key}.")

        if local_path is None:
            local_path = Path(key).name

        # Ask the webserver (in this case S3) what the expected content length is and use that.
        info_lookup = s3_client.head_object(Bucket=bucket_name, Key=key)
        length = info_lookup["ContentLength"]

        if cache and os.path.exists(local_path):
            if length is not None:
                statinfo = os.stat(local_path)
                if statinfo.st_size != length:
                    log.info(f"Found cached file {local_path} with size {statinfo.st_size} "
                             f"that is different from expected size {length}.")
                else:
                    log.info(f"Found cached file {local_path} with expected size {statinfo.st_size}.")
                    return

        with ProgressBarOrSpinner(length, (f'Downloading {self.s3_uri} to {local_path} ...')) as pb:

            # Bytes read tracks how much data has been received so far
            # This variable will be updated in multiple threads below
            global bytes_read
            bytes_read = 0

            progress_lock = threading.Lock()

            def progress_callback(numbytes):
                # Boto3 calls this from multiple threads pulling the data from S3
                global bytes_read

                # This callback can be called in multiple threads
                # Access to updating the console needs to be locked
                with progress_lock:
                    bytes_read += numbytes
                    pb.update(bytes_read)

            bkt.download_file(key, local_path, Callback=progress_callback)

    def user_on_aws(self):
        """Check if the user is in on aws
        the following works for aws, but it is not robust enough
        This is partly from: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/identify_ec2_instances.html

        Comments in user_region below are also relevant here.

        """
        uuid = '/sys/hypervisor/uuid'
        is_aws = os.path.exists(uuid) or 'AWS_REGION' in os.environ
        return True  # is_aws

    def user_region(self):
        """Find region of the user in an ec2 instance.
        There could be a way to do it with the python api instead of an http request.

        This may be complicated:
        Instance metadata (including region) can be access from the link-local address
        169.254.169.254 (https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-identity-documents.html)
        So a simple http request to http://169.254.169.254/latest/dynamic/instance-identity/document gives
        a json response that has the region info in it.

        However, in jupyterhub, that address is blocked, because it may expose sensitive information about
        the instance and kubernetes cluster
        (http://z2jh.jupyter.org/en/latest/administrator/security.html#audit-cloud-metadata-server-access).
        The region can be in $AWS_REGION
        """

        region = os.environ.get('AWS_REGION', None)

        if region is None:
            # try the link-local address
            session = requests.session()
            response = session.get('http://169.254.169.254/latest/dynamic/instance-identity/document', timeout=2)
            region = response.json()['region']

        return region
