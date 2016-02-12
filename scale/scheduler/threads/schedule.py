"""Defines the class that manages the scheduling background thread"""
from __future__ import unicode_literals

import datetime
import logging
import time

from django.db import OperationalError
from django.utils.timezone import now

from queue.job_exe import QueuedJobExecution
from queue.models import Queue
from scheduler.offer.manager import OfferManager
from util.retry import retry_database_query


logger = logging.getLogger(__name__)


try:
    from mesos.interface import mesos_pb2
    logger.info('Successfully imported native Mesos bindings')
except ImportError:
    logger.info('No native Mesos bindings, falling back to stubs')
    import mesos_api.mesos_pb2 as mesos_pb2


class SchedulingThread(object):
    """This class manages the scheduling background thread for the scheduler"""

    DELAY = 5  # In seconds
    MAX_NEW_JOB_EXES = 500  # Maximum number of new job executions to schedule per scheduling loop
    SCHEDULE_LOOP_WARN_THRESHOLD = datetime.timedelta(seconds=1)
    SCHEDULE_QUERY_WARN_THRESHOLD = datetime.timedelta(milliseconds=100)

    def __init__(self, driver, job_exe_manager, job_type_manager, node_manager, offer_manager, scheduler_manager):
        """Constructor

        :param driver: The Mesos scheduler driver
        :type driver: :class:`mesos_api.mesos.SchedulerDriver`
        :param job_exe_manager: The running job execution manager
        :type job_exe_manager: :class:`job.execution.running.manager.RunningJobExecutionManager`
        :param job_type_manager: The job type manager
        :type job_type_manager: :class:`scheduler.sync.job_type_manager.JobTypeManager`
        :param node_manager: The node manager
        :type node_manager: :class:`scheduler.sync.node_manager.NodeManager`
        :param offer_manager: The offer manager
        :type offer_manager: :class:`scheduler.offer.manager.OfferManager`
        :param scheduler_manager: The scheduler manager
        :type scheduler_manager: :class:`scheduler.sync.scheduler_manager.SchedulerManager`
        """

        self._driver = driver
        self._job_exe_manager = job_exe_manager
        self._job_type_manager = job_type_manager
        self._node_manager = node_manager
        self._offer_manager = offer_manager
        self._scheduler_manager = scheduler_manager
        self._job_types = {}
        self._running = True

    def run(self):
        """The main run loop of the thread
        """

        logger.info('Scheduling thread started')

        while self._running:

            started = now()

            num_tasks = self._perform_scheduling()

            duration = now() - started
            msg = 'Scheduling thread loop took %.3f seconds'
            if duration > SchedulingThread.SCHEDULE_LOOP_WARN_THRESHOLD:
                logger.warning(msg, duration.total_seconds())
            else:
                logger.debug(msg, duration.total_seconds())

            if num_tasks == 0:
                # Since we didn't schedule anything, give resources back to Mesos and pause a moment
                for node_offers in self._offer_manager.pop_all_offers():
                    for offer_id in node_offers.offer_ids:
                        self._driver.declineOffer(offer_id)

                logger.debug('Scheduling thread is pausing for %i second(s)', SchedulingThread.DELAY)
                time.sleep(SchedulingThread.DELAY)

        logger.info('Scheduling thread stopped')

    def shutdown(self):
        """Stops the thread from running and performs any needed clean up
        """

        logger.info('Shutting down scheduling thread')
        self._running = False

    def _consider_new_job_exes(self):
        """Considers any queued job executions for scheduling
        """

        if self._scheduler_manager.is_paused():
            return

        num_job_exes = 0
        for queue in Queue.objects.get_queue():

            if queue.job_type_id not in self._job_types or self._job_types[queue.job_type_id].is_paused:
                continue

            queued_job_exe = QueuedJobExecution(queue)
            if self._offer_manager.consider_new_job_exe(queued_job_exe) == OfferManager.ACCEPTED:
                num_job_exes += 1
                if num_job_exes >= SchedulingThread.MAX_NEW_JOB_EXES:
                    break

    def _consider_running_job_exes(self):
        """Considers any tasks for currently running job executions that are ready for the next task to run
        """

        for running_job_exe in self._job_exe_manager.get_all_job_exes():
            self._offer_manager.consider_next_task(running_job_exe)

    def _perform_scheduling(self):
        """Performs task reconciliation with the Mesos master

        :returns: The number of Mesos tasks that were scheduled
        :rtype: int
        """

        # Get updated node and job type models from managers
        self._offer_manager.update_nodes(self._node_manager.get_nodes())
        self._offer_manager.ready_new_offers()
        self._job_types = self._job_type_manager.get_job_types()

        self._consider_running_job_exes()
        self._consider_new_job_exes()

        num_tasks = self._schedule_accepted_tasks()
        logger.info('Launched %i Mesos tasks' % num_tasks)
        return num_tasks

    def _schedule_accepted_tasks(self):
        """Schedules all of the tasks that have been accepted

        :returns: The number of Mesos tasks that were scheduled
        :rtype: int
        """

        tasks_to_launch = {}  # {Node ID: [Mesos Tasks]}
        queued_job_exes_to_schedule = []
        node_offers_list = self._offer_manager.pop_offers_with_accepted_job_exes()
        for node_offers in node_offers_list:
            node_tasks = []
            tasks_to_launch[node_offers.node.id] = node_tasks
            # Start next task for already running job executions that were accepted
            for running_job_exe in node_offers.get_accepted_running_job_exes():
                task = running_job_exe.start_next_task()
                if task:
                    node_tasks.append(task)
            # Gather up queued job executions that were accepted
            for queued_job_exe in node_offers.get_accepted_new_job_exes():
                queued_job_exes_to_schedule.append(queued_job_exe)

        try:
            # Schedule queued job executions and start their first tasks
            scheduled_job_exes = self._schedule_queued_job_executions(queued_job_exes_to_schedule)
            self._job_exe_manager.add_job_exes(scheduled_job_exes)
            for scheduled_job_exe in scheduled_job_exes:
                tasks_to_launch[scheduled_job_exe.node_id].append(scheduled_job_exe.start_next_task())
        except OperationalError:
            logger.exception('Failed to schedule queued job executions')

        # Launch tasks on Mesos
        num_tasks = 0
        for node_offers in node_offers_list:
            task_list = tasks_to_launch[node_offers.node.id]
            num_tasks += len(task_list)
            self._driver.launchTasks(node_offers.offer_ids, task_list)
        return num_tasks

    @retry_database_query(max_tries=5, base_ms_delay=1000, max_ms_delay=5000)
    def _schedule_queued_job_executions(self, job_executions):
        """Schedules the given queued job executions

        :param job_executions: A list of queued job executions that have been provided nodes and resources on which to
            run
        :type job_executions: list[:class:`queue.job_exe.QueuedJobExecution`]
        :returns: The scheduled job executions
        :rtype: list[:class:`job.execution.running.job_exe.RunningJobExecution`]
        """

        started = now()

        scheduled_job_executions = Queue.objects.schedule_job_executions(job_executions)

        duration = now() - started
        msg = 'Query to schedule job executions took %.3f seconds'
        if duration > SchedulingThread.SCHEDULE_QUERY_WARN_THRESHOLD:
            logger.warning(msg, duration.total_seconds())
        else:
            logger.debug(msg, duration.total_seconds())

        return scheduled_job_executions
