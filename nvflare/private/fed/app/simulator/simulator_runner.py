# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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
import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Manager, Process
from multiprocessing.connection import Client
from urllib.parse import urlparse

from nvflare.apis.client import Client as FLClient
from nvflare.apis.fl_component import FLComponent
from nvflare.apis.fl_constant import (
    ConfigVarName,
    FLMetaKey,
    JobConstants,
    MachineStatus,
    RunnerTask,
    RunProcessKey,
    SiteType,
    SystemConfigs,
    SystemVarName,
    WorkspaceConstants,
)
from nvflare.apis.job_def import ALL_SITES, JobMetaKey
from nvflare.apis.utils.job_utils import convert_legacy_zipped_app_to_job
from nvflare.apis.workspace import Workspace
from nvflare.fuel.common.exit_codes import ProcessExitCode
from nvflare.fuel.common.multi_process_executor_constants import CommunicationMetaData
from nvflare.fuel.f3.mpm import MainProcessMonitor as mpm
from nvflare.fuel.f3.stats_pool import StatsPoolManager
from nvflare.fuel.hci.server.authz import AuthorizationService
from nvflare.fuel.sec.audit import AuditService
from nvflare.fuel.utils import log_utils
from nvflare.fuel.utils.argument_utils import parse_vars
from nvflare.fuel.utils.config_service import ConfigService
from nvflare.fuel.utils.gpu_utils import get_host_gpu_ids
from nvflare.fuel.utils.log_utils import dynamic_log_config
from nvflare.fuel.utils.network_utils import get_open_ports
from nvflare.fuel.utils.zip_utils import split_path, unzip_all_from_bytes, zip_directory_to_bytes
from nvflare.private.defs import AppFolderConstants
from nvflare.private.fed.app.deployer.simulator_deployer import SimulatorDeployer
from nvflare.private.fed.app.utils import init_security_content_service, kill_child_processes
from nvflare.private.fed.client.client_status import ClientStatus
from nvflare.private.fed.server.job_meta_validator import JobMetaValidator
from nvflare.private.fed.simulator.simulator_app_runner import SimulatorServerAppRunner
from nvflare.private.fed.simulator.simulator_audit import SimulatorAuditor
from nvflare.private.fed.simulator.simulator_const import SimulatorConstants
from nvflare.private.fed.utils.fed_utils import (
    custom_fobs_initialize,
    get_simulator_app_root,
    nvflare_fobs_initialize,
    register_ext_decomposers,
    split_gpus,
)
from nvflare.security.logging import secure_format_exception, secure_log_traceback
from nvflare.security.security import EmptyAuthorizer
from nvflare.utils.job_launcher_utils import add_custom_dir_to_path

CLIENT_CREATE_POOL_SIZE = 200
POOL_STATS_DIR = "pool_stats"
SIMULATOR_POOL_STATS = "simulator_cell_stats.json"


class SimulatorRunner(FLComponent):
    def __init__(
        self,
        job_folder: str,
        workspace: str,
        clients=None,
        n_clients=None,
        threads=None,
        gpu=None,
        log_config=None,
        max_clients=100,
        end_run_for_all=False,
    ):
        super().__init__()

        self.job_folder = job_folder
        self.workspace = workspace
        self.clients = clients
        self.n_clients = n_clients
        self.threads = threads
        self.gpu = gpu
        self.log_config = None
        self.max_clients = max_clients
        self.end_run_for_all = end_run_for_all

        self.ask_to_stop = False

        self.simulator_root = None
        self.server = None
        self.deployer = SimulatorDeployer()
        self.client_names = []
        self.federated_clients = []
        self.client_config = None
        self.deploy_args = None
        self.build_ctx = None
        self.server_custom_folder = None

        self.clients_created = 0

        running_dir = os.getcwd()
        if self.workspace is None:
            self.workspace = "simulator_workspace"
            self.logger.warning(
                f"Simulator workspace is not provided. Set it to the default location:"
                f" {os.path.join(running_dir, self.workspace)}"
            )
        self.workspace = os.path.join(running_dir, self.workspace)

        if log_config:
            log_config_path = os.path.join(running_dir, log_config)
            self.log_config = log_config_path if os.path.isfile(log_config_path) else log_config

    def _generate_args(
        self,
        job_folder: str,
        workspace: str,
        clients=None,
        n_clients=None,
        threads=None,
        gpu=None,
        log_config=None,
        max_clients=100,
    ):
        args = Namespace(
            job_folder=job_folder,
            workspace=workspace,
            clients=clients,
            n_clients=n_clients,
            threads=threads,
            gpu=gpu,
            log_config=log_config,
            max_clients=max_clients,
        )
        args.set = []
        return args

    def setup(self):
        self.args = self._generate_args(
            self.job_folder,
            self.workspace,
            self.clients,
            self.n_clients,
            self.threads,
            self.gpu,
            self.log_config,
            self.max_clients,
        )

        if self.args.clients:
            self.client_names = self.args.clients.strip().split(",")
        else:
            if self.args.n_clients:
                for i in range(self.args.n_clients):
                    self.client_names.append("site-" + str(i + 1))

        log_config_file_path = os.path.join(self.args.workspace, "local", WorkspaceConstants.LOGGING_CONFIG)
        if not os.path.isfile(log_config_file_path):
            log_config_file_path = os.path.join(os.path.dirname(log_utils.__file__), WorkspaceConstants.LOGGING_CONFIG)

        if self.args.log_config:
            log_config = self.args.log_config
        else:
            log_config = log_config_file_path

        self.args.config_folder = "config"
        self.args.job_id = SimulatorConstants.JOB_NAME
        self.args.client_config = os.path.join(self.args.config_folder, JobConstants.CLIENT_JOB_CONFIG)
        self.args.env = os.path.join("config", AppFolderConstants.CONFIG_ENV)
        cwd = os.getcwd()
        self.args.job_folder = os.path.join(cwd, self.args.job_folder)
        self.args.end_run_for_all = self.end_run_for_all

        if not os.path.exists(self.args.workspace):
            os.makedirs(self.args.workspace)
        os.chdir(self.args.workspace)
        nvflare_fobs_initialize()
        AuthorizationService.initialize(EmptyAuthorizer())
        AuditService.the_auditor = SimulatorAuditor()

        self.simulator_root = self.args.workspace
        self._cleanup_workspace()
        init_security_content_service(self.args.workspace)

        os.makedirs(os.path.join(self.simulator_root, SiteType.SERVER))

        dynamic_log_config(
            config=log_config,
            dir_path=os.path.join(self.simulator_root, SiteType.SERVER),
            reload_path=log_config_file_path,
        )

        try:
            data_bytes, job_name, meta = self.validate_job_data()

            if not self.client_names:
                self.client_names = self._extract_client_names_from_meta(meta)

            if not self.client_names:
                self.args.n_clients = 2
                self.logger.warning("The number of simulator clients is not provided. Setting it to default: 2")
                for i in range(self.args.n_clients):
                    self.client_names.append("site-" + str(i + 1))
            if self.args.gpu is None and self.args.threads is None:
                self.args.threads = 1
                self.logger.warning("The number of threads is not provided. Set it to default: 1")

            if self.max_clients < len(self.client_names):
                self.logger.error(
                    f"The number of clients ({len(self.client_names)}) can not be more than the "
                    f"max_number of clients ({self.max_clients})"
                )
                return False

            if self.args.gpu:
                try:
                    gpu_groups = split_gpus(self.args.gpu)
                except ValueError as e:
                    self.logger.error(f"GPUs group list option in wrong format. Error: {e}")
                    return False

                host_gpus = [str(x) for x in (get_host_gpu_ids())]
                gpu_ids = [x.split(",") for x in gpu_groups]
                if host_gpus and not set().union(*gpu_ids).issubset(host_gpus):
                    wrong_gpus = [x for x in gpu_groups if x not in host_gpus]
                    self.logger.error(f"These GPUs are not available: {wrong_gpus}")
                    return False

                if len(gpu_groups) > len(self.client_names):
                    self.logger.error(
                        f"The number of clients ({len(self.client_names)}) must be larger than or equal to "
                        f"the number of GPU groups: ({len(gpu_groups)})"
                    )
                    return False
                if len(gpu_groups) > 1:
                    if self.args.threads and self.args.threads > 1:
                        self.logger.info(
                            "When running with multi GPU, each GPU group will run with only 1 thread. "
                            "Set the Threads to 1."
                        )
                    self.args.threads = 1
                elif len(gpu_groups) == 1:
                    if self.args.threads is None:
                        self.args.threads = 1
                        self.logger.warning("The number of threads is not provided. Set it to default: 1")

            if self.args.threads and self.args.threads > len(self.client_names):
                self.logger.error("The number of threads to run can not be larger than the number of clients.")
                return False
            if not (self.args.gpu or self.args.threads):
                self.logger.error("Please provide the number of threads or provide gpu options to run the simulator.")
                return False

            self._validate_client_names(meta, self.client_names)

            # Deploy the FL server
            self.logger.info("Create the Simulator Server.")
            simulator_server, self.server = self.deployer.create_fl_server(self.args)
            # self.services.deploy(self.args, grpc_args=simulator_server)

            url = self.server.get_cell().get_root_url_for_child()
            parsed_url = urlparse(url)
            self.args.sp_target = parsed_url.netloc
            self.args.sp_scheme = parsed_url.scheme

            self.logger.info("Deploy the Apps.")

            # add participating job clients to meta
            job_clients = [FLClient(name, "") for name in self.client_names]
            meta[JobMetaKey.JOB_CLIENTS] = [c.to_dict() for c in job_clients]

            self._deploy_apps(job_name, data_bytes, meta, log_config_file_path)

            server_workspace = os.path.join(self.args.workspace, SiteType.SERVER)
            workspace = Workspace(root_dir=server_workspace, site_name=SiteType.SERVER)
            custom_fobs_initialize(workspace)

            decomposer_module = ConfigService.get_str_var(
                name=ConfigVarName.DECOMPOSER_MODULE, conf=SystemConfigs.RESOURCES_CONF
            )
            register_ext_decomposers(decomposer_module)

            return True

        except Exception as e:
            self.logger.error(f"Simulator setup error: {secure_format_exception(e)}")
            secure_log_traceback()
            return False

    def _cleanup_workspace(self):
        os.makedirs(self.simulator_root, exist_ok=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_dir = os.path.join(self.args.workspace, "startup")
            temp_start_up = os.path.join(temp_dir, "startup")
            local_dir = os.path.join(self.args.workspace, "local")
            temp_local_dir = os.path.join(temp_dir, "local")
            if os.path.exists(startup_dir):
                shutil.move(startup_dir, temp_start_up)
            if os.path.exists(local_dir):
                shutil.move(local_dir, temp_local_dir)

            if os.path.exists(self.simulator_root):
                shutil.rmtree(self.simulator_root)

            if os.path.exists(temp_start_up):
                shutil.move(temp_start_up, startup_dir)
            if os.path.exists(temp_local_dir):
                shutil.move(temp_local_dir, local_dir)

    def _setup_local_startup(self, log_config_file_path, workspace):
        local_dir = os.path.join(workspace, "local")
        startup = os.path.join(workspace, "startup")
        os.makedirs(local_dir, exist_ok=True)
        shutil.copyfile(log_config_file_path, os.path.join(local_dir, WorkspaceConstants.LOGGING_CONFIG))
        workspace_local = os.path.join(self.simulator_root, "local")
        if os.path.exists(workspace_local):
            shutil.copytree(workspace_local, local_dir, dirs_exist_ok=True)
        shutil.copytree(os.path.join(self.simulator_root, "startup"), startup)

    def validate_job_data(self):
        # Validate the simulate job
        job_name = split_path(self.args.job_folder)[1]
        data = zip_directory_to_bytes("", self.args.job_folder)
        data_bytes = convert_legacy_zipped_app_to_job(data)
        job_validator = JobMetaValidator()
        valid, error, meta = job_validator.validate(job_name, data_bytes)
        if not valid:
            raise RuntimeError(error)
        return data_bytes, job_name, meta

    def _extract_client_names_from_meta(self, meta):
        client_names = []
        for _, participants in meta.get(JobMetaKey.DEPLOY_MAP, {}).items():
            for p in participants:
                if p.upper() != ALL_SITES and p != SiteType.SERVER:
                    client_names.append(p)
        return client_names

    def _validate_client_names(self, meta, client_names):
        no_app_clients = []
        for name in client_names:
            name_matched = False
            for _, participants in meta.get(JobMetaKey.DEPLOY_MAP, {}).items():
                if len(participants) == 1 and participants[0].upper() == ALL_SITES:
                    name_matched = True
                    break
                if name in participants:
                    name_matched = True
                    break
            if not name_matched:
                no_app_clients.append(name)
        if no_app_clients:
            raise RuntimeError(f"The job does not have App to run for clients: {no_app_clients}")

    def _deploy_apps(self, job_name, data_bytes, meta, log_config_file_path):
        with tempfile.TemporaryDirectory() as temp_dir:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            os.mkdir(temp_dir)
            unzip_all_from_bytes(data_bytes, temp_dir)
            temp_job_folder = os.path.join(temp_dir, job_name)

            for app_name, participants in meta.get(JobMetaKey.DEPLOY_MAP).items():
                if len(participants) == 1 and participants[0].upper() == ALL_SITES:
                    participants = [SiteType.SERVER]
                    participants.extend([client for client in self.client_names])

                for p in participants:
                    if p == SiteType.SERVER or p in self.client_names:
                        app_root = get_simulator_app_root(self.simulator_root, p)
                        self._setup_local_startup(log_config_file_path, os.path.join(self.simulator_root, p))
                        app = os.path.join(temp_job_folder, app_name)
                        shutil.copytree(app, app_root)

                        job_meta_file = os.path.join(
                            self.simulator_root, p, SimulatorConstants.JOB_NAME, WorkspaceConstants.JOB_META_FILE
                        )
                        with open(job_meta_file, "w") as f:
                            json.dump(meta, f, indent=4)

    def split_clients(self, clients: [], gpus: []):
        split_clients = []
        for _ in gpus:
            split_clients.append([])
        index = 0
        for client in clients:
            split_clients[index % len(gpus)].append(client)
            index += 1
        return split_clients

    def create_clients(self):
        # Deploy the FL clients
        self.logger.info("Create the simulate clients.")
        clients_created_waiter = threading.Event()
        for client_name in self.client_names:
            self.create_client(client_name)

        self.logger.info("Set the client status ready.")
        self._set_client_status()

    def create_client(self, client_name):
        client, self.client_config, self.deploy_args, self.build_ctx = self.deployer.create_fl_client(
            client_name, self.args
        )
        self.federated_clients.append(client)

    def _set_client_status(self):
        for client in self.federated_clients:
            app_client_root = get_simulator_app_root(self.simulator_root, client.client_name)
            client.app_client_root = app_client_root
            client.args = self.args
            # self.create_client_runner(client)
            client.simulate_running = False
            client.status = ClientStatus.STARTED

    def run(self):
        try:
            manager = Manager()
            return_dict = manager.dict()
            process = Process(target=self.run_process, args=(return_dict,))
            process.start()
            process.join()
            run_status = self._get_return_code(return_dict, process, self.workspace)
            return run_status
        except KeyboardInterrupt:
            self.logger.info("KeyboardInterrupt, terminate all the child processes.")
            kill_child_processes(os.getpid())
            return -9

    def _get_return_code(self, return_dict, process, workspace):
        return_code = return_dict.get("run_status")
        if return_code:
            self.logger.info(f"process run_status: {return_code}")
        else:
            rc_file = os.path.join(workspace, FLMetaKey.PROCESS_RC_FILE)
            if os.path.exists(rc_file):
                try:
                    with open(rc_file, "r") as f:
                        return_code = int(f.readline())
                    os.remove(rc_file)
                    self.logger.info(f"return_code from process_rc_file: {return_code}")
                except Exception:
                    self.logger.warning(
                        f"Could not get the return_code from {rc_file}, Return the RC from the process:{process.pid}"
                    )
                    return_code = process.exitcode
            else:
                return_code = process.exitcode
                self.logger.info(f"return_code from process.exitcode: {return_code}")
        return return_code

    def run_process(self, return_dict):
        # run_status = self.simulator_run_main()
        try:
            run_status = mpm.run(
                main_func=self.simulator_run_main, run_dir=self.workspace, shutdown_grace_time=3, cleanup_grace_time=6
            )
        except Exception as e:
            self.logger.error(f"Simulator main run with exception: {secure_format_exception(e)}")
            run_status = ProcessExitCode.EXCEPTION

        return_dict["run_status"] = run_status

    def simulator_run_main(self):
        if self.setup():
            try:
                self.create_clients()
                self.server.engine.run_processes[SimulatorConstants.JOB_NAME] = {
                    RunProcessKey.JOB_HANDLE: None,
                    RunProcessKey.JOB_ID: SimulatorConstants.JOB_NAME,
                    RunProcessKey.PARTICIPANTS: self.server.engine.client_manager.clients,
                }

                self.logger.info("Deploy and start the Server App.")
                args = copy.deepcopy(self.args)
                server_thread = threading.Thread(target=self.start_server_app, args=[args])
                server_thread.start()

                # wait for the server app is started
                while self.server.engine.engine_info.status != MachineStatus.STARTED:
                    time.sleep(1.0)
                    if not server_thread.is_alive():
                        raise RuntimeError("Could not start the Server App.")

                # # Start the client heartbeat calls.
                # for client in self.federated_clients:
                #     client.start_heartbeat(interval=2)

                if self.args.gpu:
                    gpus = split_gpus(self.args.gpu)
                    split_clients = self.split_clients(self.federated_clients, gpus)
                else:
                    gpus = [None]
                    split_clients = [self.federated_clients]

                executor = ThreadPoolExecutor(max_workers=len(gpus))
                for index in range(len(gpus)):
                    clients = split_clients[index]
                    executor.submit(lambda p: self.client_run(*p), [self.server_custom_folder, clients, gpus[index]])

                executor.shutdown()
                # Abort the server after all clients finished run
                self.server.abort_run()
                server_thread.join()
                run_status = 0
            except Exception as e:
                self.logger.error(f"Simulator run error: {secure_format_exception(e)}")
                run_status = 2
            finally:
                # self.services.close()
                self.deployer.close()
        else:
            run_status = 1
        return run_status

    def client_run(self, server_custom_folder, clients, gpu):
        client_runner = SimulatorClientRunner(
            server_custom_folder, self.args, clients, self.client_config, self.deploy_args, self.build_ctx
        )
        client_runner.run(gpu)

    def start_server_app(self, args):
        app_server_root = os.path.join(self.simulator_root, "server", SimulatorConstants.JOB_NAME, "app_server")
        args.workspace = os.path.join(self.simulator_root, "server")
        os.chdir(args.workspace)

        args.server_config = os.path.join("config", JobConstants.SERVER_JOB_CONFIG)
        self.server_custom_folder = os.path.join(app_server_root, "custom")
        if os.path.isdir(self.server_custom_folder) and self.server_custom_folder not in sys.path:
            sys.path.append(self.server_custom_folder)

        startup = os.path.join(args.workspace, WorkspaceConstants.STARTUP_FOLDER_NAME)
        os.makedirs(startup, exist_ok=True)
        local = os.path.join(args.workspace, WorkspaceConstants.SITE_FOLDER_NAME)
        os.makedirs(local, exist_ok=True)
        workspace = Workspace(root_dir=args.workspace, site_name=SiteType.SERVER)

        self.server.job_cell = self.server.create_job_cell(
            SimulatorConstants.JOB_NAME,
            self.server.get_cell().get_root_url_for_child(),
            self.server.get_cell().get_internal_listener_url(),
            False,
            None,
        )
        server_app_runner = SimulatorServerAppRunner(self.server)
        snapshot = None
        kv_list = [f"secure_train={self.server.secure_train}"]
        server_app_runner.start_server_app(
            workspace, args, app_server_root, args.job_id, snapshot, self.logger, kv_list=kv_list
        )

        # start = time.time()
        # while self.services.engine.client_manager.clients:
        #     # Wait for the clients to shutdown and quite first.
        #     time.sleep(0.1)
        #     if time.time() - start > 30.:
        #         break

        self.dump_stats(workspace)

        self.server.admin_server.stop()
        self.server.close()

    def dump_stats(self, workspace: Workspace):
        stats_dict = StatsPoolManager.to_dict()
        json_object = json.dumps(stats_dict, indent=4)
        os.makedirs(os.path.join(workspace.get_root_dir(), POOL_STATS_DIR), exist_ok=True)
        file = os.path.join(workspace.get_root_dir(), POOL_STATS_DIR, SIMULATOR_POOL_STATS)
        with open(file, "w") as outfile:
            outfile.write(json_object)


class SimulatorClientRunner(FLComponent):
    def __init__(self, server_custom_folder, args, clients: [], client_config, deploy_args, build_ctx):
        super().__init__()
        self.server_custom_folder = server_custom_folder
        self.args = args
        self.federated_clients = clients
        self.run_client_index = -1

        self.simulator_root = self.args.workspace
        self.client_config = client_config
        self.deploy_args = deploy_args
        self.build_ctx = build_ctx
        self.kv_list = parse_vars(args.set)
        self.logging_config = os.path.join(self.args.workspace, "local", WorkspaceConstants.LOGGING_CONFIG)

        self.clients_finished_end_run = []

    def run(self, gpu):
        try:
            # self.create_clients()
            self.logger.info("Start the clients run simulation.")
            executor = ThreadPoolExecutor(max_workers=self.args.threads)
            lock = threading.Lock()
            timeout = self.kv_list.get("simulator_worker_timeout", 60.0)
            for i in range(self.args.threads):
                executor.submit(
                    lambda p: self.run_client_thread(*p),
                    [self.args.threads, gpu, lock, self.args.end_run_for_all, timeout],
                )

            # wait for the server and client running thread to finish.
            executor.shutdown()

        except Exception as e:
            self.logger.error(f"SimulatorClientRunner run error: {secure_format_exception(e)}")
        finally:
            for client in self.federated_clients:
                threading.Thread(target=self._shutdown_client, args=[client]).start()

    def _shutdown_client(self, client):
        try:
            client.communicator.heartbeat_done = True
            # time.sleep(3)
            client.terminate()
            # client.close()
            client.status = ClientStatus.STOPPED
            client.communicator.cell.stop()
        except:
            # Ignore the exception for the simulator client shutdown
            self.logger.warning(f"Exception happened to client{client.name} during shutdown ")

    def run_client_thread(self, num_of_threads, gpu, lock, end_run_for_all, timeout=60):
        stop_run = False
        interval = 1
        client_to_run = None  # indicates the next client to run

        try:
            while not stop_run:
                time.sleep(interval)
                with lock:
                    if not client_to_run:
                        client = self.get_next_run_client(gpu)
                    else:
                        client = client_to_run

                client.simulate_running = True
                stop_run, client_to_run, end_run_client = self.do_one_task(
                    client, num_of_threads, gpu, lock, timeout=timeout
                )
                if end_run_client:
                    with lock:
                        self.clients_finished_end_run.append(end_run_client)

                client.simulate_running = False

            if end_run_for_all:
                self._end_run_clients(gpu, lock, num_of_threads, timeout)
        except Exception as e:
            self.logger.error(f"run_client_thread error: {secure_format_exception(e)}")

    def _end_run_clients(self, gpu, lock, num_of_threads, timeout):
        """After the WF reaches the END_RUN, each running thread will try to pick up one of the remaining client
        which has not run the END_RUN yet, then execute the END_RUN handler, until all the clients have done so.
        These client END_RUN event handler only execute when "end_run_for_all" has been set.

        Multiple client running threads will try to pick up the client from the same clients pool.

        """
        # Each thread only stop picking up the NOT-DONE client until all clients have run the END_RUN event.
        while len(self.clients_finished_end_run) != len(self.federated_clients):
            with lock:
                end_run_client = self._pick_next_client()
            if end_run_client:
                self.do_one_task(
                    end_run_client, num_of_threads, gpu, lock, timeout=timeout, task_name=RunnerTask.END_RUN
                )
                with lock:
                    end_run_client.simulate_running = False

    def _pick_next_client(self):
        for client in self.federated_clients:
            # Ensure the client has not run the END_RUN event
            if client.client_name not in self.clients_finished_end_run and not client.simulate_running:
                client.simulate_running = True
                self.clients_finished_end_run.append(client.client_name)
                return client
        return None

    def do_one_task(self, client, num_of_threads, gpu, lock, timeout=60.0, task_name=RunnerTask.TASK_EXEC):
        open_port = get_open_ports(1)[0]
        client_workspace = os.path.join(self.args.workspace, client.client_name)

        log_config_file_path = os.path.join(self.args.workspace, "local", WorkspaceConstants.LOGGING_CONFIG)
        if not os.path.isfile(log_config_file_path):
            log_config_file_path = os.path.join(os.path.dirname(log_utils.__file__), WorkspaceConstants.LOGGING_CONFIG)

        if self.args.log_config:
            logging_config = self.args.log_config
        else:
            logging_config = log_config_file_path

        decomposer_module = ConfigService.get_str_var(
            name=ConfigVarName.DECOMPOSER_MODULE, conf=SystemConfigs.RESOURCES_CONF
        )

        app_custom_folder = Workspace(root_dir=client_workspace, site_name="mgh").get_app_custom_dir(
            SimulatorConstants.JOB_NAME
        )

        command = (
            sys.executable
            + " -m nvflare.private.fed.app.simulator.simulator_worker -o "
            + client_workspace
            + " --logging_config "
            + logging_config
            + " --client "
            + client.client_name
            + " --token "
            + client.token
            + " --port "
            + str(open_port)
            + " --parent_pid "
            + str(os.getpid())
            + " --simulator_root "
            + self.simulator_root
            + " --root_url "
            + str(client.cell.get_root_url_for_child())
            + " --parent_url "
            + str(client.cell.get_internal_listener_url())
            + " --task_name "
            + str(task_name)
            + " --decomposer_module "
            + str(decomposer_module)
        )
        if gpu:
            command += " --gpu " + str(gpu)
        new_env = os.environ.copy()
        add_custom_dir_to_path(app_custom_folder, new_env)
        if os.path.isdir(self.server_custom_folder):
            python_paths = new_env[SystemVarName.PYTHONPATH].split(os.pathsep)
            if self.server_custom_folder in python_paths:
                python_paths.remove(self.server_custom_folder)
            new_env[SystemVarName.PYTHONPATH] = os.pathsep.join(python_paths)

        _ = subprocess.Popen(shlex.split(command, True), preexec_fn=os.setsid, env=new_env)

        conn = self._create_connection(open_port, timeout=timeout)

        self.build_ctx["client_name"] = client.client_name
        deploy_args = copy.deepcopy(self.deploy_args)
        deploy_args.workspace = os.path.join(deploy_args.workspace, client.client_name)
        data = {
            # SimulatorConstants.CLIENT: client,
            SimulatorConstants.CLIENT_CONFIG: self.client_config,
            SimulatorConstants.DEPLOY_ARGS: deploy_args,
            SimulatorConstants.BUILD_CTX: self.build_ctx,
        }
        conn.send(data)

        end_run_client = None
        while True:
            stop_run = conn.recv()
            if stop_run:
                end_run_client = conn.recv()

            with lock:
                if num_of_threads != len(self.federated_clients):
                    next_client = self.get_next_run_client(gpu)
                else:
                    next_client = client
            if not stop_run and next_client.client_name == client.client_name:
                conn.send(True)
            else:
                conn.send(False)
                break

        return stop_run, next_client, end_run_client

    def _get_new_sys_path(self):
        new_sys_path = []
        for i in range(0, len(sys.path) - 1):
            if sys.path[i]:
                new_sys_path.append(sys.path[i])
        return new_sys_path

    def _create_connection(self, open_port, timeout=60.0):
        conn = None
        start = time.time()
        while not conn:
            try:
                address = ("localhost", open_port)
                conn = Client(address, authkey=CommunicationMetaData.CHILD_PASSWORD.encode())
            except Exception:
                if time.time() - start > timeout:
                    raise RuntimeError(
                        f"Failed to create connection to the child process in {self.__class__.__name__},"
                        f" timeout: {timeout}"
                    )
                time.sleep(1.0)
                pass
        return conn

    def get_next_run_client(self, gpu):
        # Find the next client which is not currently running
        while True:
            self.run_client_index = (self.run_client_index + 1) % len(self.federated_clients)
            client = self.federated_clients[self.run_client_index]
            if not client.simulate_running:
                break
        self.logger.info(f"Simulate Run client: {client.client_name} on GPU group: {gpu}")
        return client
