"""Defines the database models related to ingesting files"""
from __future__ import unicode_literals

import datetime
import logging

import django.utils.timezone as timezone
import djorm_pgjson.fields
from django.db import models, transaction
from django.utils.timezone import now

from ingest.scan.configuration.scan_configuration import ScanConfiguration
from ingest.strike.configuration.strike_configuration import StrikeConfiguration
from job.configuration.data.job_data import JobData
from job.models import JobType
from queue.models import Queue
from storage.exceptions import InvalidDataTypeTag
from storage.models import VALID_TAG_PATTERN
from trigger.models import TriggerEvent

logger = logging.getLogger(__name__)


class IngestCounts(object):
    """Represents ingest status values for a specific time slot.

    :keyword time: The time slot being counted.
    :type time: datetime.datetime
    :keyword files: The number of files ingested for the time slot.
    :type files: int
    :keyword size: The total size of all files ingested for the time slot in bytes.
    :type size: int
    """

    def __init__(self, time, files=0, size=0):
        self.time = time
        self.files = files
        self.size = size


class IngestStatus(object):
    """Represents ingest status values for a strike process.

    :keyword strike: The strike process that generated the ingests being counted.
    :type strike: :class:`strike.models.Strike`
    :keyword most_recent: The date/time of the last ingest generated by the strike process.
    :type most_recent: datetime.datetime
    :keyword files: The total number of files ingested by the strike process.
    :type files: int
    :keyword size: The total size of all files ingested by the strike process in bytes.
    :type size: int
    :keyword values: A list of values that summarize work done by the strike process.
    :type values: list[:class:`ingest.models.IngestCounts`]
    """

    def __init__(self, strike, most_recent=None, files=0, size=0, values=None):
        self.strike = strike
        self.most_recent = most_recent
        self.files = files
        self.size = size
        self.values = values or []


class IngestManager(models.Manager):
    """Provides additional methods for handling ingests."""

    def get_ingest_job_type(self):
        """Returns the Scale Ingest job type

        :returns: The ingest job type
        :rtype: :class:`job.models.JobType`
        """

        return JobType.objects.get(name='scale-ingest', version='1.0')

    def get_ingests(self, started=None, ended=None, statuses=None, strike_ids=None, file_name=None, order=None):
        """Returns a list of ingests within the given time range.

        :param started: Query ingests updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query ingests updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param statuses: Query ingests with the a specific process status.
        :type statuses: [string]
        :param strike_ids: Query ingests created by a specific strike processor.
        :type strike_ids: list[string]
        :param file_name: Query ingests with the a specific file name.
        :type file_name: string
        :param order: A list of fields to control the sort order.
        :type order: list[string]
        :returns: The list of ingests that match the time range.
        :rtype: list[:class:`ingest.models.Ingest`]
        """

        # Fetch a list of ingests
        ingests = Ingest.objects.all()
        ingests = ingests.select_related('strike', 'source_file', 'source_file__workspace')
        ingests = ingests.defer('strike__configuration', 'source_file__workspace__json_config')

        # Apply time range filtering
        if started:
            ingests = ingests.filter(last_modified__gte=started)
        if ended:
            ingests = ingests.filter(last_modified__lte=ended)

        if statuses:
            ingests = ingests.filter(status__in=statuses)
        if strike_ids:
            ingests = ingests.filter(strike_id__in=strike_ids)
        if file_name:
            ingests = ingests.filter(file_name=file_name)

        # Apply sorting
        if order:
            ingests = ingests.order_by(*order)
        else:
            ingests = ingests.order_by('last_modified')
        return ingests

    def get_ingests_by_scan(self, scan_id, file_names=None):
        """Returns a list of ingests associated with a scan and optionally files

        :param scan_id: Query ingests created by a specific scan processor.
        :type scan_id: list[string]
        :param file_names: Query ingests with the specific file names.
        :type file_names: list[string]
        :returns: The list of ingests that match the scan and file_names.
        :rtype: list[:class:`ingest.models.Ingest`]
        """

        # Fetch a list of ingests
        ingests = Ingest.objects.all()

        if scan_id:
            ingests = ingests.filter(scan_id=scan_id)
        if file_names:
            ingests = ingests.filter(file_name__in=file_names)

        return ingests

    def get_details(self, ingest_id):
        """Gets additional details for the given ingest model based on related model attributes.

        :param ingest_id: The unique identifier of the ingest.
        :type ingest_id: int
        :returns: The ingest with extra related attributes.
        :rtype: :class:`ingest.models.Ingest`
        """

        # Attempt to fetch the requested ingest
        ingest = Ingest.objects.all().select_related('strike', 'strike__job', 'strike__job__job_type',
                                                     'source_file', 'source_file__workspace')
        ingest = ingest.defer('source_file__workspace__json_config')
        ingest = ingest.get(pk=ingest_id)

        return ingest

    def get_status(self, started=None, ended=None, use_ingest_time=False):
        """Returns ingest status information within the given time range grouped by strike process.

        :param started: Query ingests updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query ingests updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param use_ingest_time: Whether or not to group the status values by ingest time (False) or data time (True).
        :type use_ingest_time: bool
        :returns: The list of ingest status models that match the time range.
        :rtype: list[:class:`ingest.models.IngestStatus`]
        """

        # Fetch a list of ingests
        ingests = Ingest.objects.filter(status='INGESTED')
        ingests = ingests.select_related('strike')
        ingests = ingests.defer('strike__configuration')

        # Apply time range filtering
        if started:
            if use_ingest_time:
                ingests = ingests.filter(ingest_ended__gte=started)
            else:
                ingests = ingests.filter(data_ended__gte=started)
        if ended:
            if use_ingest_time:
                ingests = ingests.filter(ingest_ended__lte=ended)
            else:
                ingests = ingests.filter(data_started__lte=ended)

        # Apply sorting
        if use_ingest_time:
            ingests = ingests.order_by('ingest_ended')
        else:
            ingests = ingests.order_by('data_started')

        groups = self._group_by_time(ingests, use_ingest_time)
        return [self._fill_status(status, time_slots, started, ended) for status, time_slots in groups.iteritems()]

    def _group_by_time(self, ingests, use_ingest_time):
        """Groups the given ingests by hourly time slots.

        :param ingests: Query ingests updated after this amount of time.
        :type ingests: list[:class:`ingest.models.Ingest`]
        :param use_ingest_time: Whether or not to group the status values by ingest time (False) or data time (True).
        :type use_ingest_time: bool
        :returns: A mapping of ingest status models to hourly groups of counts.
        :rtype: dict[:class:`ingest.models.IngestStatus`, dict[datetime.datetime, :class:`ingest.models.IngestCounts`]]
        """

        # Build a mapping of all possible strike processes
        strike_map = {}
        slot_map = {}
        for strike in Strike.objects.all():
            strike_map[strike] = IngestStatus(strike)
            slot_map[strike] = {}

        # Build a mapping of ingest status to time slots
        for ingest in ingests:

            # Initialize the mappings for the first strike
            if ingest.strike not in strike_map:
                logger.error('Missing strike process mapping: %s', ingest.strike_id)
                continue

            # Check whether there is a valid date for the requested query
            dated = ingest.ingest_ended if use_ingest_time else ingest.data_started
            if dated:
                ingest_status = strike_map[ingest.strike]
                time_slots = slot_map[ingest.strike]
                self._update_status(ingest_status, time_slots, ingest, dated)

        return {strike_map[strike]: slot_map[strike] for strike in strike_map}

    def _update_status(self, ingest_status, time_slots, ingest, dated):
        """Updates the given ingest status model based on attributes of an ingest model.

        :param ingest_status: The ingest status to update.
        :type ingest_status: :class:`ingest.models.IngestStatus`
        :param time_slots: A mapping of hourly time slots to ingest status counts.
        :type time_slots: dict[datetime.datetime, :class:`ingest.models.IngestCounts`]
        :param ingest: The ingest model that should be counted.
        :type ingest: :class:`ingest.models.Ingest`
        :returns: The ingest status model after the counts are updated.
        :rtype: :class:`ingest.models.IngestStatus`
        """

        # Calculate the hourly time slot the record falls within
        time_slot = datetime.datetime(dated.year, dated.month, dated.day, dated.hour, tzinfo=timezone.utc)

        # Update the values for the current time slot
        if time_slot not in time_slots:
            time_slots[time_slot] = IngestCounts(time_slot)
        values = time_slots[time_slot]
        values.files += 1
        values.size += ingest.file_size

        # Update the summary values for the ingest status
        ingest_status.files += 1
        ingest_status.size += ingest.file_size
        if not ingest_status.most_recent or dated > ingest_status.most_recent:
            ingest_status.most_recent = dated

        return ingest_status

    def _fill_status(self, ingest_status, time_slots, started=None, ended=None):
        """Fills all the values for the given ingest status using a specified time range and grouped values.

        This method ensures that each hourly bin has a value, even when no data actually exists.

        :param ingest_status: The ingest status to fill with values.
        :type ingest_status: :class:`ingest.models.IngestStatus`
        :param time_slots: A mapping of hourly time slots to ingest status counts.
        :type time_slots: dict[datetime.datetime, :class:`ingest.models.IngestCounts`]
        :param started: The start of the time range that needs to be filled.
        :type started: datetime.datetime
        :param ended: The end of the time range that needs to be filled.
        :type ended: datetime.datetime
        :returns: The ingest status model after the values array is filled.
        :rtype: :class:`ingest.models.IngestStatus`
        """

        # Make sure we have a valid time range
        started = started if started else datetime.datetime.combine(timezone.now().date(), datetime.time.min)
        ended = ended if ended else datetime.datetime.combine(timezone.now().date(), datetime.time.max)

        # Build a list of values for each hourly time slot including zero value place holders where needed
        duration = ended.date() - started.date()
        for day in range(duration.days + 1):
            for hour in range(24):
                dated = started + datetime.timedelta(days=day)
                time_slot = datetime.datetime(dated.year, dated.month, dated.day, hour, tzinfo=timezone.utc)
                status_vals = time_slots[time_slot] if time_slot in time_slots else IngestCounts(time_slot)
                ingest_status.values.append(status_vals)

        return ingest_status


class Ingest(models.Model):
    """Represents an instance of a file being ingested into a workspace

    :keyword file_name: The name of the file
    :type file_name: :class:`django.db.models.CharField`
    :keyword strike: The Strike process that created this ingest
    :type strike: :class:`django.db.models.ForeignKey`
    :keyword status: The status of the file ingest process
    :type status: :class:`django.db.models.CharField`

    :keyword transfer_started: When the transfer to the workspace started
    :type transfer_started: :class:`django.db.models.DateTimeField`
    :keyword transfer_ended: When the transfer to the workspace ended
    :type transfer_ended: :class:`django.db.models.DateTimeField`
    :keyword bytes_transferred: The total number of bytes transferred so far
    :type bytes_transferred: :class:`django.db.models.BigIntegerField`

    :keyword media_type: The IANA media type of the file
    :type media_type: :class:`django.db.models.CharField`
    :keyword file_size: The size of the file in bytes
    :type file_size: :class:`django.db.models.BigIntegerField`
    :keyword data_type: A comma-separated string listing the data type "tags" for the file
    :type data_type: :class:`django.db.models.TextField`

    :keyword file_path: The relative path for where the file is stored in the workspace
    :type file_path: :class:`django.db.models.CharField`
    :keyword workspace: The workspace where the file was transferred
    :type workspace: :class:`django.db.models.ForeignKey`
    :keyword new_file_path: The relative path for where the file should be moved as part of ingesting
    :type new_file_path: :class:`django.db.models.CharField`
    :keyword new_workspace: The new workspace to move the file into as part of ingesting
    :type new_workspace: :class:`django.db.models.ForeignKey`

    :keyword job: The ingest job that is processing this ingest
    :type job: :class:`django.db.models.ForeignKey`
    :keyword ingest_started: When the ingest was started
    :type ingest_started: :class:`django.db.models.DateTimeField`
    :keyword ingest_ended: When the ingest ended
    :type ingest_ended: :class:`django.db.models.DateTimeField`
    :keyword source_file: A reference to the source file that was stored by this ingest
    :type source_file: :class:`django.db.models.ForeignKey`
    :keyword data_started: The start time of the data in this source file
    :type data_started: :class:`django.db.models.DateTimeField`
    :keyword data_ended: The end time of the data in this source file
    :type data_ended: :class:`django.db.models.DateTimeField`

    :keyword created: When the ingest model was created
    :type created: :class:`django.db.models.DateTimeField`
    :keyword last_modified: When the ingest model was last modified
    :type last_modified: :class:`django.db.models.DateTimeField`
    """
    INGEST_STATUSES = (
        ('TRANSFERRING', 'TRANSFERRING'),
        ('TRANSFERRED', 'TRANSFERRED'),
        ('DEFERRED', 'DEFERRED'),
        ('QUEUED', 'QUEUED'),
        ('INGESTING', 'INGESTING'),
        ('INGESTED', 'INGESTED'),
        ('ERRORED', 'ERRORED'),
        ('DUPLICATE', 'DUPLICATE'),
    )

    file_name = models.CharField(max_length=250, db_index=True)
    strike = models.ForeignKey('ingest.Strike', on_delete=models.PROTECT, null=True)
    scan = models.ForeignKey('ingest.Scan', on_delete=models.PROTECT, null=True)
    status = models.CharField(choices=INGEST_STATUSES, default='TRANSFERRING', max_length=50, db_index=True)

    bytes_transferred = models.BigIntegerField(blank=True, null=True)
    transfer_started = models.DateTimeField(blank=True, null=True)
    transfer_ended = models.DateTimeField(blank=True, null=True)

    media_type = models.CharField(max_length=250, blank=True)
    file_size = models.BigIntegerField(blank=True, null=True)
    data_type = models.TextField(blank=True)

    file_path = models.CharField(max_length=1000, blank=True)
    workspace = models.ForeignKey('storage.Workspace', blank=True, null=True, related_name='+')
    new_file_path = models.CharField(max_length=1000, blank=True)
    new_workspace = models.ForeignKey('storage.Workspace', blank=True, null=True, related_name='+')

    job = models.ForeignKey('job.Job', blank=True, null=True)
    ingest_started = models.DateTimeField(blank=True, null=True)
    ingest_ended = models.DateTimeField(blank=True, null=True, db_index=True)

    source_file = models.ForeignKey('storage.ScaleFile', blank=True, null=True)
    data_started = models.DateTimeField(blank=True, null=True, db_index=True)
    data_ended = models.DateTimeField(blank=True, null=True, db_index=True)

    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    objects = IngestManager()

    def add_data_type_tag(self, tag):
        """Adds a new data type tag to the file. A valid tag contains only alphanumeric characters, underscores, and
        spaces.

        :param tag: The data type tag to add
        :type tag: string
        :raises InvalidDataTypeTag: If the given tag is invalid
        """

        if not VALID_TAG_PATTERN.match(tag):
            raise InvalidDataTypeTag('%s is an invalid data type tag' % tag)

        tags = self.get_data_type_tags()
        tags.add(tag)
        self._set_data_type_tags(tags)

    def get_data_type_tags(self):
        """Returns the set of data type tags associated with this file

        :returns: The set of data type tags
        :rtype: set of string
        """

        tags = set()
        if self.data_type:
            for tag in self.data_type.split(','):
                tags.add(tag)
        return tags

    def _set_data_type_tags(self, tags):
        """Sets the data type tags on the model

        :param tags: The data type tags
        :type tags: set of string
        """

        self.data_type = ','.join(tags)

    class Meta(object):
        """meta information for database"""
        db_table = 'ingest'


class ScanManager(models.Manager):
    """Provides additional methods for handling Scan processes
    """

    @transaction.atomic
    def create_scan(self, name, title, description, configuration, dry_run=True):
        """Creates a new Scan process with the given configuration and returns the new Scan model. The Scan model
        will be saved in the database and the job to run the Scan process will be placed on the queue. All changes to
        the database will occur in an atomic transaction.

        :param name: The identifying name of this Scan process
        :type name: string
        :param title: The human-readable name of this Scan process
        :type title: string
        :param description: A description of this Scan process
        :type description: string
        :param configuration: The Scan configuration
        :type configuration: dict
        :param dry_run: Whether the scan will execute as a dry run
        :type dry_run: bool
        :returns: The new Scan process
        :rtype: :class:`ingest.models.Scan`

        :raises :class:`ingest.scan.configuration.exceptions.InvalidScanConfiguration`: If the configuration is
            invalid.
        """

        # Validate the configuration, no exception is success
        config = ScanConfiguration(configuration)
        config.validate()

        scan = Scan()
        scan.name = name
        scan.title = title
        scan.description = description
        scan.configuration = config.get_dict()
        scan.save()

        scan_type = self.get_scan_job_type()
        job_data = JobData()
        job_data.add_property_input('Scan ID', unicode(scan.id))
        job_data.add_property_input('Dry Run', str(dry_run))
        event_description = {'scan_id': scan.id}
        event = TriggerEvent.objects.create_trigger_event('SCAN_CREATED', None, event_description, now())
        scan.job = Queue.objects.queue_new_job(scan_type, job_data, event)
        scan.save()

        return scan

    @transaction.atomic
    def edit_scan(self, scan_id, title=None, description=None, configuration=None):
        """Edits the given Scan process and saves the changes in the database. All database changes occur in an atomic
        transaction. An argument of None for a field indicates that the field should not change.

        :param scan_id: The unique identifier of the Scan process to edit
        :type scan_id: int
        :param title: The human-readable name of this Scan process
        :type title: string
        :param description: A description of this Scan process
        :type description: string
        :param configuration: The Strike process configuration
        :type configuration: dict

        :raises :class:`ingest.scan.configuration.exceptions.InvalidScanConfiguration`: If the configuration is
            invalid.
        """

        scan = Scan.objects.get(pk=scan_id)

        # Validate the configuration, no exception is success
        if configuration:
            config = ScanConfiguration(configuration)
            config.validate()
            scan.configuration = config.get_dict()

        # Update editable fields
        if title:
            scan.title = title
        if description:
            scan.description = description
        scan.save()

    def get_scan_job_type(self):
        """Returns the Scale Scan job type

        :returns: The Scan job type
        :rtype: :class:`job.models.JobType`
        """

        return JobType.objects.get(name='scale-scan', version='1.0')

    def get_scans(self, started=None, ended=None, names=None, order=None):
        """Returns a list of Scan processes within the given time range.

        :param started: Query Scan processes updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query Scan processes updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param names: Query Scan processes associated with the name.
        :type names: list[string]
        :param order: A list of fields to control the sort order.
        :type order: list[string]
        :returns: The list of Scan processes that match the time range.
        :rtype: list[:class:`ingest.models.Scan`]
        """

        # Fetch a list of strikes
        scans = Scab.objects.select_related('job', 'job__job_type').defer('configuration')

        # Apply time range filtering
        if started:
            scans = scans.filter(last_modified__gte=started)
        if ended:
            scans = scans.filter(last_modified__lte=ended)

        # Apply additional filters
        if names:
            scans = scans.filter(name__in=names)

        # Apply sorting
        if order:
            scans = scans.order_by(*order)
        else:
            scans = scans.order_by('last_modified')
        return scans

    def get_details(self, scan_id):
        """Returns the Scan process for the given ID with all detail fields included.

        :param scan_id: The unique identifier of the Scan process.
        :type scan_id: int
        :returns: The Scan process with all detail fields included.
        :rtype: :class:`ingest.models.Scan`
        """

        return Scan.objects.select_related('job', 'job__job_type').get(pk=scan_id)


class Scan(models.Model):
    """Represents an instance of a Scan process which will run and detect files
    in a workspace for ingest

    :keyword name: The identifying name of this Scan process
    :type name: :class:`django.db.models.CharField`
    :keyword title: The human-readable name of this Scan process
    :type title: :class:`django.db.models.CharField`
    :keyword description: An optional description of this Scan process
    :type description: :class:`django.db.models.CharField`

    :keyword configuration: JSON configuration for this Scan process
    :type configuration: :class:`djorm_pgjson.fields.JSONField`
    :keyword job: The job that is performing the Scan process
    :type job: :class:`django.db.models.ForeignKey`

    :keyword created: When the Scan process was created
    :type created: :class:`django.db.models.DateTimeField`
    :keyword last_modified: When the Scan process was last modified
    :type last_modified: :class:`django.db.models.DateTimeField`
    """

    name = models.CharField(max_length=50, unique=True)
    title = models.CharField(blank=True, max_length=50, null=True)
    description = models.CharField(blank=True, max_length=500)

    configuration = djorm_pgjson.fields.JSONField()
    job = models.ForeignKey('job.Job', blank=True, null=True, on_delete=models.PROTECT)

    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    objects = ScanManager()

    def get_scan_configuration(self):
        """Returns the configuration for this Scan process

        :returns: The configuration for this Scan process
        :rtype: :class:`ingest.scan.configuration.scan_configuration.ScanConfiguration`
        """

        return ScanConfiguration(self.configuration)

    def get_scan_configuration_as_dict(self):
        """Returns the configuration for this Scan process as a dict

        :returns: The configuration for this Scan process
        :rtype: dict
        """

        return self.get_scan_configuration().get_dict()

    class Meta(object):
        """meta information for database"""
        db_table = 'scan'


class StrikeManager(models.Manager):
    """Provides additional methods for handling Strike processes
    """

    @transaction.atomic
    def create_strike(self, name, title, description, configuration):
        """Creates a new Strike process with the given configuration and returns the new Strike model. The Strike model
        will be saved in the database and the job to run the Strike process will be placed on the queue. All changes to
        the database will occur in an atomic transaction.

        :param name: The identifying name of this Strike process
        :type name: string
        :param title: The human-readable name of this Strike process
        :type title: string
        :param description: A description of this Strike process
        :type description: string
        :param configuration: The Strike configuration
        :type configuration: dict
        :returns: The new Strike process
        :rtype: :class:`ingest.models.Strike`

        :raises :class:`ingest.strike.configuration.exceptions.InvalidStrikeConfiguration`: If the configuration is
            invalid.
        """

        # Validate the configuration, no exception is success
        config = StrikeConfiguration(configuration)
        config.validate()

        strike = Strike()
        strike.name = name
        strike.title = title
        strike.description = description
        strike.configuration = config.get_dict()
        strike.save()

        strike_type = self.get_strike_job_type()
        job_data = JobData()
        job_data.add_property_input('Strike ID', unicode(strike.id))
        event_description = {'strike_id': strike.id}
        event = TriggerEvent.objects.create_trigger_event('STRIKE_CREATED', None, event_description, now())
        strike.job = Queue.objects.queue_new_job(strike_type, job_data, event)
        strike.save()

        return strike

    @transaction.atomic
    def edit_strike(self, strike_id, title=None, description=None, configuration=None):
        """Edits the given Strike process and saves the changes in the database. All database changes occur in an atomic
        transaction. An argument of None for a field indicates that the field should not change.

        :param strike_id: The unique identifier of the Strike process to edit
        :type strike_id: int
        :param title: The human-readable name of this Strike process
        :type title: string
        :param description: A description of this Strike process
        :type description: string
        :param configuration: The Strike process configuration
        :type configuration: dict

        :raises :class:`ingest.strike.configuration.exceptions.InvalidStrikeConfiguration`: If the configuration is
            invalid.
        """

        strike = Strike.objects.get(pk=strike_id)

        # Validate the configuration, no exception is success
        if configuration:
            config = StrikeConfiguration(configuration)
            config.validate()
            strike.configuration = config.get_dict()

        # Update editable fields
        if title:
            strike.title = title
        if description:
            strike.description = description
        strike.save()

    def get_strike_job_type(self):
        """Returns the Scale Strike job type

        :returns: The Strike job type
        :rtype: :class:`job.models.JobType`
        """

        return JobType.objects.get(name='scale-strike', version='1.0')

    def get_strikes(self, started=None, ended=None, names=None, order=None):
        """Returns a list of Strike processes within the given time range.

        :param started: Query Strike processes updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query Strike processes updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param names: Query Strike processes associated with the name.
        :type names: list[string]
        :param order: A list of fields to control the sort order.
        :type order: list[string]
        :returns: The list of Strike processes that match the time range.
        :rtype: list[:class:`ingest.models.Strike`]
        """

        # Fetch a list of strikes
        strikes = Strike.objects.select_related('job', 'job__job_type').defer('configuration')

        # Apply time range filtering
        if started:
            strikes = strikes.filter(last_modified__gte=started)
        if ended:
            strikes = strikes.filter(last_modified__lte=ended)

        # Apply additional filters
        if names:
            strikes = strikes.filter(name__in=names)

        # Apply sorting
        if order:
            strikes = strikes.order_by(*order)
        else:
            strikes = strikes.order_by('last_modified')
        return strikes

    def get_details(self, strike_id):
        """Returns the Strike process for the given ID with all detail fields included.

        :param strike_id: The unique identifier of the Strike process.
        :type strike_id: int
        :returns: The Strike process with all detail fields included.
        :rtype: :class:`ingest.models.Strike`
        """

        return Strike.objects.select_related('job', 'job__job_type').get(pk=strike_id)


class Strike(models.Model):
    """Represents an instance of a Strike process which will run and detect incoming files in a directory for ingest

    :keyword name: The identifying name of this Strike process
    :type name: :class:`django.db.models.CharField`
    :keyword title: The human-readable name of this Strike process
    :type title: :class:`django.db.models.CharField`
    :keyword description: An optional description of this Strike process
    :type description: :class:`django.db.models.CharField`

    :keyword configuration: JSON configuration for this Strike process
    :type configuration: :class:`djorm_pgjson.fields.JSONField`
    :keyword job: The job that is performing the Strike process
    :type job: :class:`django.db.models.ForeignKey`

    :keyword created: When the Strike process was created
    :type created: :class:`django.db.models.DateTimeField`
    :keyword last_modified: When the Strike process was last modified
    :type last_modified: :class:`django.db.models.DateTimeField`
    """

    name = models.CharField(max_length=50, unique=True)
    title = models.CharField(blank=True, max_length=50, null=True)
    description = models.CharField(blank=True, max_length=500)

    configuration = djorm_pgjson.fields.JSONField()
    job = models.ForeignKey('job.Job', blank=True, null=True, on_delete=models.PROTECT)

    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    objects = StrikeManager()

    def get_strike_configuration(self):
        """Returns the configuration for this Strike process

        :returns: The configuration for this Strike process
        :rtype: :class:`ingest.strike.configuration.strike_configuration.StrikeConfiguration`
        """

        return StrikeConfiguration(self.configuration)

    def get_strike_configuration_as_dict(self):
        """Returns the configuration for this Strike process as a dict

        :returns: The configuration for this Strike process
        :rtype: dict
        """

        return self.get_strike_configuration().get_dict()

    class Meta(object):
        """meta information for database"""
        db_table = 'strike'
