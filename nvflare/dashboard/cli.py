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

import argparse
import os
import signal
import subprocess
import sys

import docker
import nvflare
from nvflare.apis.utils.format_check import name_check
from nvflare.lighter import tplt_utils, utils

supported_csp = ("azure", "aws")


def start(args):
    cwd = os.getcwd()
    if not args.folder:
        folder = cwd
    else:
        folder = os.path.join(cwd, args.folder)
    environment = dict()
    env_vars = args.env
    if env_vars:
        for e in env_vars:
            splitted = e.split("=")
            environment[splitted[0]] = splitted[1]
    passphrase = args.passphrase
    if passphrase:
        environment["NVFL_DASHBOARD_PP"] = passphrase
    if args.cred:
        environment.update({"NVFL_CREDENTIAL": args.cred})
    elif not os.path.exists(os.path.join(folder, ".db_init_done")):
        need_email = True
        while need_email:
            answer = input(
                "Please provide project admin email address.  This person will be the super user of the dashboard and this project.\n"
            )
            error, reason = name_check(answer, "email")
            if error:
                print(f"Expecting an email address, but got one in an invalid format.  Reason: {reason}")
            else:
                need_email = False
        email = answer
        need_org = True
        while need_org:
            answer = input("Please provide project admin organization name.\n")
            error, reason = name_check(answer, "org")
            if error:
                print(f"Expecting an organization name, but got one in an invalid format.  Reason: {reason}")
            else:
                need_org = False
        org_name = answer
        print("generating random password")
        pwd = utils.generate_password(8)
        print(f"Project admin credential is {email} and the password is {pwd}")
        environment.update({"NVFL_CREDENTIAL": f"{email}:{pwd}:{org_name}"})
    if args.local:
        return start_local(environment)
    try:
        client = docker.from_env()
    except docker.errors.DockerException:
        print("Unable to communicate to docker daemon/socket.  Please make sure your docker is up and running.")
        exit(0)
    version = nvflare.__version__
    dashboard_image = f"nvflare/nvflare:{version}"
    if args.image:
        if dashboard_image != args.image:
            print(
                f"Current dashboard container image is nvflare/nvflare:{version}, but requesting to use {args.image}.  Use it at your own risk."
            )
            dashboard_image = args.image
    try:
        print(f"Pulling {dashboard_image}, may take some time to finish.")
        _ = client.images.pull(dashboard_image)
    except docker.errors.APIError:
        print(f"unable to pull {dashboard_image}")
        exit(1)
    print(f"Launching {dashboard_image}")
    print(f"Dashboard will listen to port {args.port}")
    print(f"{folder} on host mounted to /var/tmp/nvflare/dashboard in container")
    if environment:
        print(f"environment vars set to {environment}")
    else:
        print("No additional environment variables set to the launched container.")
    try:
        container_obj = client.containers.run(
            dashboard_image,
            entrypoint=["/usr/local/bin/python3", "nvflare/dashboard/wsgi.py"],
            detach=True,
            auto_remove=True,
            name="nvflare-dashboard",
            ports={8443: args.port},
            volumes={folder: {"bind": "/var/tmp/nvflare/dashboard", "model": "rw"}},
            environment=environment,
        )
    except docker.errors.APIError as e:
        print(f"Either {dashboard_image} image does not exist or another nvflare-dashboard instance is still running.")
        print("Please either provide an existing container image or stop the running container instance.")
        print(e)
        exit(1)
    if container_obj:
        print("Dashboard container started")
        print("Container name nvflare-dashboard")
        print(f"id is {container_obj.id}")
    else:
        print("Container failed to start")


def start_local(env):
    print("Local dashboard without docker is for development and test only")
    file_dir_path = os.path.dirname(__file__)
    wsgi_location = os.path.join(file_dir_path, "wsgi.py")
    cmd = [sys.executable, wsgi_location]
    env.update({"NVFL_WEB_ROOT": os.path.dirname(os.path.abspath(__file__))})
    process_status = subprocess.run(args=cmd, env=env)
    return process_status


def stop():
    try:
        client = docker.from_env()
    except docker.errors.DockerException:
        print("Unable to communicate to docker daemon/socket.  Please make sure your docker is up and running.")
        exit(0)
    try:
        container_obj = client.containers.get("nvflare-dashboard")
    except docker.errors.NotFound:
        print("No nvflare-dashboard container found")
        exit(0)
    container_obj.kill(signal=signal.SIGINT)
    print("nvflare-dashboard exited")


def cloud(args):
    lighter_folder = os.path.dirname(utils.__file__)
    template = utils.load_yaml(os.path.join(lighter_folder, "templates", "master_template.yml"))
    template.update(utils.load_yaml(os.path.join(lighter_folder, "templates", "aws_template.yml")))
    template.update(utils.load_yaml(os.path.join(lighter_folder, "templates", "azure_template.yml")))
    tplt = tplt_utils.Template(template)
    cwd = os.getcwd()
    csp = args.cloud
    dest = os.path.join(cwd, f"{csp}_start_dsb.sh")
    dsb_start = template[f"{csp}_start_dsb_sh"]
    version = nvflare.__version__
    if "+" in version:
        print(
            f"Unable to launching dashboard on cloud with {version}.  Please install official NVFlare release from PyPi."
        )
        exit(0)
    replacement_dict = {"NVFLARE": f"nvflare=={version}", "START_OPT": f"-i {args.image}" if args.image else ""}
    utils._write(
        dest,
        utils.sh_replace(tplt.get_cloud_script_header() + dsb_start, replacement_dict),
        "t",
        exe=True,
    )
    print(f"Dashboard launch script for cloud is written at {dest}.  Now running it.")
    if args.vpc_id and args.subnet_id:
        option = [f"--vpc-id={args.vpc_id}", f"--subnet-id={args.subnet_id}"]
        print(f"Option of the script: {option}")
        dest = [dest] + option
    _ = subprocess.run(dest)
    os.remove(dest)


def has_no_arguments() -> bool:
    last_item = sys.argv[-1]
    return (
        last_item.endswith("dashboard.cli") or last_item.endswith("dashboard/cli.py") or last_item.endswith("dashboard")
    )


def main():
    parser = argparse.ArgumentParser()
    define_dashboard_parser(parser)
    args = parser.parse_args()
    handle_dashboard(args)


def define_dashboard_parser(parser):
    parser.add_argument(
        "--cloud",
        type=str,
        default="",
        help="launch dashboard on cloud service provider (ex: --cloud azure or --cloud aws)",
    )
    parser.add_argument("--start", action="store_true", help="start dashboard")
    parser.add_argument("--stop", action="store_true", help="stop dashboard")
    parser.add_argument("-p", "--port", type=str, default="443", help="port to listen")
    parser.add_argument(
        "-f", "--folder", type=str, help="folder containing necessary info (default: current working directory)"
    )
    parser.add_argument(
        "--passphrase", help="Passphrase to encrypt/decrypt root CA private key.  !!! Do not share it with others. !!!"
    )
    parser.add_argument("-e", "--env", action="append", help="additonal environment variables: var1=value1")
    parser.add_argument("--cred", help="set credential directly in the form of USER_EMAIL:PASSWORD")
    parser.add_argument("-i", "--image", help="set the container image name")
    parser.add_argument("--local", action="store_true", help="start dashboard locally without docker image")
    parser.add_argument(
        "--vpc-id",
        type=str,
        default="",
        help="VPC id for AWS EC2 instance.  Applicable to AWS only.  Ignored if subnet-id is not specified.",
    )
    parser.add_argument(
        "--subnet-id",
        type=str,
        default="",
        help="Subnet id for AWS EC2 instance.  Applicable to AWS only.  Ignored if vpc-id is not specified.",
    )


def handle_dashboard(args):
    support_csp_string = ", ".join(supported_csp)
    if args.stop:
        stop()
    elif args.start or args.local:
        start(args)
    elif args.cloud:
        if args.cloud in supported_csp:
            cloud(args)
        else:
            print(
                f"Currently --cloud support the following options: {support_csp_string}.  However, {args.cloud} is requested."
            )
    else:
        print("Please use -h option to see usage")


if __name__ == "__main__":
    main()
