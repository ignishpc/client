import sys
import os
import json
import docker
import base64

import ignis.deploy.utils as utils

IMAGE_NAME = "ignishpc/nomad"
MODULE_NAME = "nomad"
CONTAINER_NAME = "ignis-nomad"
CONTAINER_DATA = "/var/lib/ignis/nomad/"


def start(bind, partner, ports, password, config_file, name, data, no_client, no_server, docker_bin, default_registry,
          force, clear):
    try:
        if config_file is None:
            config = dict()
        else:
            with open(config_file) as file:
                config = json.load(file)

        if no_client and no_server:
            print("error: " + CONTAINER_NAME + " no_agent and no_server can not be used together")
            exit(-1)

        client = docker.from_env()
        container = utils.getContainer(client, CONTAINER_NAME)
        if container:
            if force:
                container.remove(force=True)
            else:
                print("error: " + CONTAINER_NAME + " already exists")
                exit(-1)

        if bind is None:
            bind = utils.getHostname()
            print("info: " + bind + " selected for internal cluster communications, use --bind to select another")

        if password is None:
            password = utils.randomPassword()

        if name is None:
            name = bind

        if data is None:
            data = CONTAINER_DATA
        if clear:
            utils.rmIfExists(data)

        utils.mkdirIfNotExists(data)

        if docker_bin is None:
            docker_bin = "/usr/bin/docker"

        mounts = [
            docker.types.Mount(source=data, target="/var/lib/nomad", type="bind"),
            docker.types.Mount(source="/var/run/docker.sock", target="/var/run/docker.sock", type="bind"),
            docker.types.Mount(source="/tmp", target="/tmp", type="bind"),
            docker.types.Mount(source=docker_bin, target="/usr/bin/docker", type="bind"),
        ]

        environment = {
        }

        rconfig = _rConfig(config)

        rconfig["name"] = name
        rconfig["bind_addr"] = "0.0.0.0"
        rconfig["advertise"] = {
            "http": bind + (":" + str(ports[0]) if ports else ""),
            "rpc": bind + (":" + str(ports[1]) if ports else ""),
            "serf": bind + (":" + str(ports[1]) if ports else "")
        }
        rconfig["data_dir"] = "/var/lib/nomad"
        rconfig["datacenter"] = "ignis"
        rconfig["server"]["enabled"] = not no_server
        rconfig["server"]["encrypt"] = utils.sha256base64(password)
        rconfig["client"]["enabled"] = not no_client
        rconfig["client"]["servers"] = [rconfig["advertise"]["http"]]

        if partner:
            rconfig["client"]["servers"].append(partner)
            rconfig["server"]["retry_join"] = [partner]
        elif "bootstrap_expect" not in config["server"]:
            rconfig["server"]["bootstrap_expect"] = 1

        with open(os.path.join(data, "config.json"), "w") as file:
            json.dump(config, file, indent=4, sort_keys=True)
        command = ["nomad", "agent", "-config", "/var/lib/nomad/config.json"]

        container = client.containers.run(
            image=default_registry + IMAGE_NAME,
            name=CONTAINER_NAME,
            detach=True,
            environment=environment,
            privileged=True,
            command=command,
            mounts=mounts,
            network_mode="host",
            pid_mode="host"
        )
    except PermissionError:
        print("root required!!", file=sys.stderr)
        sys.exit(-1)
    except Exception as ex:
        print("error:  " + str(ex), file=sys.stderr)
        exit(-1)


def status():
    client = docker.from_env()
    return utils.getStatus(client, CONTAINER_NAME)


def resume():
    client = docker.from_env()
    utils.containerAction(client, CONTAINER_NAME, MODULE_NAME, lambda container: container.start())


def stop():
    client = docker.from_env()
    utils.containerAction(client, CONTAINER_NAME, MODULE_NAME, lambda container: container.stop())


def destroy():
    client = docker.from_env()
    utils.containerAction(client, CONTAINER_NAME, MODULE_NAME, lambda container: container.remove(force=True))


class _rConfig:
    def __init__(self, config):
        self.__config = config

    def __getitem__(self, key):
        if key not in self.__config:
            self.__config[key] = {}
        value = self.__config[key]
        if type(value) == dict:
            return _rConfig(value)
        else:
            return value

    def __setitem__(self, key, value):
        self.__config[key] = value