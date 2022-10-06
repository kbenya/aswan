from functools import partial
from multiprocessing import Process, cpu_count
from typing import Dict, Iterable, List, Optional, Type

from atqo import DEFAULT_DIST_API_KEY, DEFAULT_MULTI_API, Scheduler
from structlog import get_logger

from .connection_session import HandlingTask, get_actor_dict
from .constants import Statuses
from .depot import AswanDepot, Status
from .models import CollEvent, RegEvent, SourceUrl
from .object_store import ObjectStore
from .resources import REnum
from .url_handler import UrlHandlerBase
from .utils import is_subclass, run_and_log_functions

logger = get_logger()


class Project:
    def __init__(
        self,
        name: str,
        local_root: Optional[str] = None,
        min_queue_size=20,
        batch_size=40,
        distributed_api=DEFAULT_MULTI_API,
        debug=False,
    ):

        self.depot = AswanDepot(name, local_root)
        self.object_store = ObjectStore(self.depot.object_store_path)
        self.min_queue_size = min_queue_size
        self.batch_size = batch_size
        self.distributed_api = distributed_api
        self.debug = debug

        self._handler_dic: Dict[str, "UrlHandlerBase"] = {}
        self._scheduler: Optional[Scheduler] = None
        self._monitor_app_process: Optional[Process] = None

        self._ran_once = False
        self._keep_running = True
        self._is_test = False

    def run(
        self,
        urls_to_register: Optional[Dict[Type, Iterable[str]]] = None,
        urls_to_overwrite: Optional[Dict[Type, Iterable[str]]] = None,
        test_run=False,
        keep_running=True,
        force_sync=False,
    ):
        """run project

        test runs on a basic local thread
        """

        self._ran_once = False
        self._is_test = test_run

        _prep = [
            self.depot.setup,
            partial(self._initiate_status, urls_to_register, urls_to_overwrite),
        ]
        self._run(force_sync, keep_running, _prep)

    def commit_current_run(self):
        if self._is_test:
            raise PermissionError("last run was a test, do not commit it")
        if self.depot.current.any_in_progress():
            raise ValueError()
        # TODO: check if commit hash is same?

        self.depot.save_current()
        self.cleanup_current_run()

    def cleanup_current_run(self):
        self.depot.current.purge()

    def continue_run(
        self,
        inprogress=True,
        parsing_error=False,
        conn_error=False,
        sess_broken=False,
        force_sync=False,
        keep_running=True,
    ):
        bool_map = {
            Statuses.PROCESSING: inprogress,
            Statuses.PARSING_ERROR: parsing_error,
            Statuses.CONNECTION_ERROR: conn_error,
            Statuses.SESSION_BROKEN: sess_broken,
        }
        statuses = [s for s, b in bool_map.items() if b]
        prep = [partial(self.depot.current.reset_surls, statuses)]
        self._run(force_sync, keep_running, prep)

    def register_handler(self, handler: Type["UrlHandlerBase"]):
        # called for .name to work later and proxy to init
        self._handler_dic[handler.__name__] = handler()
        return handler

    def register_module(self, mod):
        for e in mod.__dict__.values():
            if is_subclass(e, UrlHandlerBase):
                self.register_handler(e)

    def start_monitor_process(self, port_no=6969):
        # to avoid extra deps
        from .monitor_app import run_monitor_app

        self._monitor_app_process = Process(
            target=run_monitor_app,
            kwargs={"port_no": port_no, "depot": self.depot},
        )
        self._monitor_app_process.start()
        logger.info(f" monitor app at: http://localhost:{port_no}")

    def stop_monitor_process(self):
        self._monitor_app_process.terminate()
        self._monitor_app_process.join()

    def handler_events(
        self,
        handler: Type["UrlHandlerBase"],
        only_successful: bool = True,
        only_latest: bool = True,
        limit=float("inf"),
        past_run_count=0,
    ) -> Iterable["ParsedCollectionEvent"]:
        for cev in self.depot.get_handler_events(
            handler.__name__, only_successful, only_latest, limit, past_run_count
        ):
            yield ParsedCollectionEvent(cev.extend(), self)

    @property
    def resource_limits(self):
        proxy_limits = {
            k: p.max_at_once for k, p in self._proxy_dic.items() if p.max_at_once
        }
        # TODO stupid literals
        return {
            REnum.mCPU: int(cpu_count() * 1000),
            REnum.DISPLAY: 4,
            **proxy_limits,
        }

    def _run(self, force_sync, keep_running, extra_prep=()):
        self._keep_running = keep_running
        _old_da = self.distributed_api
        if force_sync:
            self.distributed_api = DEFAULT_DIST_API_KEY
        run_and_log_functions([*extra_prep, self._create_scheduler], batch="prep")
        self._scheduler.process(
            batch_producer=self._get_next_batch,
            result_processor=self.depot.current.process_results,
            min_queue_size=self.min_queue_size,
        )
        run_and_log_functions([self._scheduler.join], batch="cleanup")
        self.distributed_api = _old_da

    def _get_next_batch(self):
        if self._ran_once and not self._keep_running:
            return []
        n_to_target = self.batch_size - self._scheduler.queued_task_count
        self._ran_once = True
        return self.depot.current.next_batch(
            max(n_to_target, 0), to_processing=True, parser=self._surls_to_tasks
        )

    def _surls_to_tasks(self, surl_batch: List[SourceUrl]):
        return [
            HandlingTask(
                handler=self._handler_dic[next_surl.handler],
                url=next_surl.url,
                object_store=self.object_store,
            ).get_scheduler_task()
            for next_surl in surl_batch
        ]

    def _initiate_status(
        self,
        urls_to_register: Optional[Dict[Type[UrlHandlerBase], Iterable[str]]],
        urls_to_overwrite: Optional[Dict[Type[UrlHandlerBase], Iterable[str]]],
    ):
        reg_events = []
        for url_dic, ovw in [(urls_to_register, False), (urls_to_overwrite, True)]:
            for handler, urls in (url_dic or {}).items():
                reg_events.extend(_get_event_bunch(handler, urls, ovw))

        if self._is_test:
            status = Status()
            for handler in self._handler_dic.values():
                reg_events.extend(_get_event_bunch(type(handler), handler.test_urls))
        else:
            status = self.depot.get_complete_status()

        self.depot.set_as_current(status)
        self.depot.current.integrate_events(reg_events)

    def _create_scheduler(self):
        self._scheduler = Scheduler(
            actor_dict=get_actor_dict(self._proxy_dic.values()),
            resource_limits=self.resource_limits,
            distributed_system=self.distributed_api,  # TODO move test to sync?
            verbose=self.debug,
        )

    @property
    def _proxy_dic(self):
        return {
            handler.proxy.res_id: handler.proxy
            for handler in self._handler_dic.values()
            if handler.proxy
        }


class ParsedCollectionEvent:
    def __init__(self, cev: "CollEvent", project: Project):
        self.url = cev.url
        self.handler_name = cev.handler
        self.output_file = cev.output_file
        self._ostore = project.object_store
        self._time = cev.timestamp
        self.status = cev.status

    @property
    def content(self):
        return self._ostore.read(self.output_file) if self.output_file else None

    def __repr__(self):
        return f"{self.status}: {self.handler_name} - {self.url} ({self._time})"


def _get_event_bunch(handler: Type[UrlHandlerBase], urls, overwrite=False):
    part = partial(RegEvent, handler=handler.__name__, overwrite=overwrite)
    return map(part, map(handler.extend_link, urls))
