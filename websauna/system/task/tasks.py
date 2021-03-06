"""Transaction-aware Celery task handling.

Inspired by Warehouse project https://raw.githubusercontent.com/pypa/warehouse/master/warehouse/celery.py
"""

import logging

import transaction
import venusian
from celery import Task
from pyramid.request import apply_request_extensions
from pyramid.scripting import _make_request
from pyramid_tm import tm_tween_factory
from transaction import TransactionManager


from websauna.system.task.celery import get_celery
from websauna.system.http import Request


logger = logging.getLogger(__name__)


class WebsaunaTask(Task):
    """A task that can clean up its transaction at the end."""

    def get_request(self, **options) -> Request:
        """Get the current HTTPRequest interface associated with the task.

        This is not a real HTTP request - Celery is not connected to HTTP interface. Instead, a faux request is generated by :py:class:`websauna.system.task.celeryloader.WebsaunaLoader`.
        """
        # This is set by on_task_init in loader
        request = getattr(self.request, "request", None)
        if request:
            return request

        return request

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """What happens if a task raises exception.

        """
        # Should be logged by properly configured Celery itself
        # logger.error("Celery task failure %s, args %s, kwargs %s", task_id, args, kwargs)
        pass

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        """Clean up transaction after task run."""

        if not self.request.is_eager:
            # Close the request when task completes
            request = self.get_request()

            # Make sure tasks don't leave transaction open e.g. in the case of exception
            if status == "FAILURE":
                logger.debug("Closing request task %s, status %s", self, status)
                tm = request.transaction_manager
                txn = tm._txn
                if txn:
                    txn.abort()
            else:
                logger.debug("Finished request task %s, status %s", self, status)
                # This will terminate dbsession, as set in create_transaction_manager_aware_dbsession

            # Call add-on hooks
            from websauna.system.task.events import TaskFinished  # Avoid circular imports
            request.registry.notify(TaskFinished(request, self))

            request._process_finished_callbacks()


class ScheduleOnCommitTask(WebsaunaTask):
    """A Celery task that does not get scheduled to execution until the current transaction commits.

    This is a :py:class:`celery.app.task.Task` based class to be used with :py:meth:`celery.Celery.task` function decorator.

    The created task only executes through ``apply_async`` if the web transaction successfully commits and only after transaction successfully commits. Thus, it is safe to pass ids to any database objects for the task and expect the task to be able to read them.
    """

    def make_faux_request(self):
        """In the case we can't use real request object, make a new request from registry given to Celery."""
        # Real request has been already committed in this point,
        # so create a faux request to satisfy the presence of dbsession et. al.
        registry = self.app.registry
        request = _make_request("/", registry)
        apply_request_extensions(request)
        return request

    def get_transaction_manager(self, **options) -> TransactionManager:
        """Get the transaction manager we are bound to."""

        tm = options.get("tm")

        if not tm:
            raise RuntimeError("You need to explicitly pass transaction manager as 'tm' task option to ScheduleOnCommitTask. Task keyword arguments are are: {}".format(options))

        return tm

    def exec_eager(self, *args, **kwargs):
        """Run transaction aware task in eager mode."""
        self.request.update(request=self.make_faux_request())
        return self.run(*args, **kwargs)

    def __call__(self, *args, **kwargs):
        """Call Celery task and insert request argument.

        Wrap Celery task call for better exception handling.

        Celery itself does very bad job in logging exceptions. So LET'S "#€!"€! STOP SILENTLY SWALLOWING THEM. This is for eager.
        """

        if self.request.is_eager:
            return self.exec_eager(*args, **kwargs)

        try:
            underlying = super().__call__
            return underlying(*args, **kwargs)
        except Exception as e:
            logger.error("Celery task raised an exception %s", e)
            logger.exception(e)
            raise
        finally:
            # TODO? Do we need closer?
            # pyramid_env["closer"]()
            pass

    def apply_async_on_commit(self, *args, **kwargs):
        """Schedule a task from web process."""

        tm = self.get_transaction_manager(**kwargs)
        kwargs.pop("tm")

        # This will break things that expect to get an AsyncResult because
        # we're no longer going to be returning an async result from this when
        # called from within a request, response cycle. Ideally we shouldn't be
        # waiting for responses in a request/response cycle anyways though.
        tm.get().addAfterCommitHook(
            self._after_commit_hook,
            args=args,
            kws=kwargs,
        )

    def apply_async_instant(self, *args, **options):
        """Schedule async task from beat process."""
        return super().apply_async(*args, **options)

    def apply_async(self, *args, **options):

        if "producer" in options:
            # This comes from Celery beat process.
            # Celery beat doesn't know about transaction lifecycles.
            # Instantly schedule the task.
            return self.apply_async_instant(*args, **options)
        else:
            # This call comes from inside a web process and
            # we only want to make the task run on commit
            return self.apply_async_on_commit(*args, **options)

    def _after_commit_hook(self, success, *args, **kwargs):
        """When HTTP request terminates and the transaction is committed, actually submit the task to Celery."""
        if success:
            super().apply_async(*args, **kwargs)


class RetryableTransactionTask(ScheduleOnCommitTask):
    """Celery task that commits all the work at the end of the task using transaction manager commit.

    A base class to be used with :py:meth:`celery.Celery.task` function decorator. Automatically commits all the work at the end of the task.

    In the case of transaction conflict, the task will rerun based on :func:`pyramid_tm.tm_tween_factory` attempt rules.
    """

    abstract = True

    def exec_eager(self, *args, **kwargs):
        """Run transaction aware task in eager mode."""
        self.request.update(request=self.make_faux_request())

        with transaction.manager:
            # This doesn't do transaction retry attempts, but should be good enough for eager
            return self.run(*args, **kwargs)

    def __call__(self, *args, **kwargs):

        # Get bound Task.__call__
        # http://stackoverflow.com/a/1015405/315168
        underlying = Task.__call__.__get__(self, Task)

        if self.request.is_eager:
            return self.exec_eager(*args, **kwargs)

        request = self.get_request()

        try:
            def handler(request):
                try:
                    return underlying(*args, **kwargs)
                except Exception as e:
                    logger.error("TransactionalTask task raised an exception %s", e)
                    logger.exception(e)
                    raise

            handler = tm_tween_factory(handler, request.registry)
            result = handler(request)
        finally:
            # TODO: Do we need closer?
            # pyramid_env["closer"]()
            pass

        return result


class TaskProxy:
    """Late-bind Celery tasks to decorated functions.

    Normally ``celery.task()`` binds everything during import time. But we want to avoid this, as we don't want to deal with any configuration during import time.

    We wrap a decorated function with this proxy. Then we forward all the calls to Celery Task object after it has been bound during the end of configuration.
    """

    def __init__(self, original_func):
        self.original_func = original_func
        self.celery_task = None

        # Venusian setup
        self.__venusian_callbacks__ = None
        self.__name__ = self.original_func.__name__

    def __str__(self):
        return "TaskProxy for {} bound to task {}".format(self.original_func, self.celery_task)

    def __repr__(self):
        return self.__str__()

    def __call__(self, *args, **kwargs):
        raise RuntimeError("Tasked functions should not be directly called. Instead use apply_async() and other Celery task functions to initiate them")

    def bind_celery_task(self, celery_task: Task):
        assert isinstance(celery_task, Task)
        self.celery_task  = celery_task

    def __getattr__(self, item):
        """Resolve all method calls to the underlying task."""

        if not self.celery_task:
            raise RuntimeError("Celery task creation failed. Did config.scan() do a sweep on {}? TaskProxy tried to look up attribute: {}".format(self.original_func, item))

        return getattr(self.celery_task, item)


def task(*args, **kwargs):
    """Configuration compatible task decorator.

    Tasks are picked up by :py:meth:`pyramid.config.Configurator.scan` run on the module, not during import time.
    Otherwise we mimic the behavior of :py:meth:`celery.Celery.task`.

    :param args: Passed to Celery task decorator
    :param kwargs: Passed to Celery task decorator
    """

    def _inner(func):
        "The class decorator example"

        proxy = TaskProxy(func)

        def register(scanner, name, task_proxy):
            config = scanner.config
            registry = config.registry
            celery = get_celery(registry)
            celery_task = celery.task(task_proxy.original_func, *args, **kwargs)
            proxy.bind_celery_task(celery_task)

        venusian.attach(proxy, register, category='celery')
        return proxy

    return _inner


