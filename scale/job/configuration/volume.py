"""Defines a Docker volume that will be mounted into a container"""
from __future__ import unicode_literals

from job.configuration.job_parameter import DockerParam

MODE_RO = 'ro'
MODE_RW = 'rw'


class Volume(object):
    """Defines a Docker volume that will be mounted into a container
    """

    def __init__(self, container_path, mode, is_host=True, host_path=None, name=None, driver=None, driver_opts=None):
        """Creates a volume to be mounted into a container

        :param container_path: The path within the container onto which the volume will be mounted
        :type container_path: string
        :param mode: Either 'ro' for read-only or 'rw' for read-write
        :type mode: string
        :param is_host: True if this is a host mount, False if this is a normal volume
        :type is_host: bool
        :param host_path: The path on the host to mount into the container
        :type host_path: string
        :param name: The name of the volume
        :type name: string
        :param driver: The volume driver to use
        :type driver: string
        :param driver_opts: The driver options to use
        :type driver_opts: dict
        """

        self.container_path = container_path
        self.mode = mode
        self.is_host = is_host
        self.host_path = host_path
        self.name = name
        self.driver = driver
        self.driver_opts = driver_opts

    def to_docker_param(self):
        """Returns a Docker parameter that will perform the mount of this volume

        :returns: The Docker parameter that will mount this volume
        :rtype: :class:`job.configuration.job_parameter.DockerParam`
        """

        # TODO: this currently only supports creating a volume for the first time
        if self.is_host:
            # Host mount is special, no volume name, just the host path
            volume_name = self.host_path
        else:
            # Create named volume, possibly with driver and driver options
            driver_params = []
            if self.driver:
                driver_params.append('--driver %s' % self.driver)
            if self.driver_opts:
                for name, value in self.driver_opts.iteritems():
                    driver_params.append('--opt %s=%s' % (name, value))
            if driver_params:
                volume_name = '$(docker volume create --name %s %s)' % (self.name, ' '.join(driver_params))
            else:
                volume_name = '$(docker volume create --name %s)' % self.name

        volume_param = '%s:%s:%s' % (volume_name, self.container_path, self.mode)
        return DockerParam('volume', volume_param)
