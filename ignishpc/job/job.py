import os
import subprocess
from multiprocessing import Process
import tempfile
import sys
import threading
import io
import base64

import docker
import docker.types
import docker.errors

from ignishpc.common import configuration


def _run(args):
    if args.cmd == "run":
        _job_run(args)
    else:
        return {
            "run": _job_run,
            "list": _list,
            "info": _info,
            "cancel": _cancel,
        }[args.action](args)


def _docker_stdin(c, con=None):
    if con is None:
        con = c.attach_socket(params={"stdin": True, "stream": True})
        return threading.Thread(target=_docker_stdin, daemon=True, args=[c, con]).start()

    sock = con if hasattr(con, "send") else con._sock
    try:
        for l in sys.stdin:
            sock.send(l.encode("utf-8"))
    except:
        pass
    finally:
        try:
            sock.close()
        except:
            pass


def _container_job(args, it):
    wdir = configuration.get_string("ignis.wdir")
    writable = configuration.get_bool("ignis.container.writable")
    network = configuration.network()
    env = {}
    binds = []

    dsocket = "/var/run/docker.sock"
    if configuration.get_string("ignis.container.provider") == "docker" and os.path.exists(dsocket):
        configuration.set_property(f"ignis.submitter.binds.{dsocket}", dsocket)

    configuration.set_property(f"ignis.submitter.binds.{os.path.abspath(wdir)}", os.path.abspath(wdir))

    buffer = io.BytesIO()
    configuration.yaml.dump(configuration.props, buffer)
    env["IGNIS_OPTIONS"] = base64.b64encode(buffer.getvalue()).decode("utf-8")

    prop_binds = configuration.get_property("ignis.submitter.binds", {})

    def getBinds(p, prefix=""):
        if hasattr(prop_binds, "items"):
            for key, value in p.items():
                if isinstance(value, str):
                    if ":" in value:
                        value, ro = value.split(":")
                        if value == "":
                            value = key
                        key += ":" + ro
                    binds.append(value + ":" + prefix + key)
                elif hasattr(prop_binds, "items"):
                    getBinds(value, key + ".")

    getBinds(prop_binds)

    prop_env = configuration.get_property("ignis.submitter.env", {})
    if hasattr(prop_env, "items"):
        for key, value in prop_env.items():
            if isinstance(value, str):
                env[key] = value

    provider = configuration.get_string("ignis.container.provider")
    if provider != "docker":
        cmd = [provider, "exec", "--cleanenv"]

        if wdir is not None:
            cmd.extend(["--workdir", wdir])

        if writable:
            cmd.append("--writable-tmpfs")

        for key, val in env.items():
            cmd.extend(["--env", f"{key}={val}"])

        if network != "default":
            cmd.extend(["--net", "--network", network])

        for bind in binds:
            cmd.extend(["--bind", bind])

        proc = subprocess.Popen(
            args=cmd + [configuration.default_image(), "ignis-submit"] + args,
            stdin=sys.stdin if it else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            cwd=wdir,
            encoding="utf-8",
        )

        proc.stdin.close()

        for line in iter(proc.stdout.readline, ""):
            print(line, end="", flush=True)

        return_code = proc.wait()

    else:
        root = configuration.get_bool("ignis.container.docker.root")
        other_args = {}

        if network in ("host", "bridge", "none"):
            other_args["network_mode"] = network
        elif network != "default":
            other_args["network"] = network

        if wdir is not None:
            other_args["working_dir"] = wdir

        container = None

        def to_mount(f):
            if ":" not in f:
                return docker.types.Mount(f, f, type="bind")
            fields = f.split(":")
            return docker.types.Mount(source=fields[0], target=fields[1], type="bind",
                                      read_only=len(fields) > 2 and fields[2] == "ro")

        key_sock = "ignis.submitter.binds./var/run/docker.sock"
        group_add = []
        if not root and sys.platform.startswith("darwin"):
            result = docker.from_env().containers.run(
                image="alpine:3.19",
                remove=True,
                read_only=True,
                stdout=True,
                mounts=[to_mount("/var/run/docker.sock")],
                command=["ls", "-l", "/var/run/docker.sock"]
            ).decode("UTF-8")
            group_add.append(result.split()[3])

        elif not root and configuration.has_property(key_sock):
            group_add.append(os.stat(configuration.get_property(key_sock)).st_gid)

        try:
            container = docker.from_env().containers.create(
                image=configuration.default_image(),
                command=["ignis-submit"] + args,
                environment=env,
                mounts=[to_mount(bind) for bind in binds],
                read_only=not writable,
                user="root" if root else "{}:{}".format(os.getuid(), os.getgid()),
                group_add=group_add,
                stdin_open=it,
                **other_args
            )
            container.start()
            if it:
                _docker_stdin(container)

            output = container.logs(stdout=True, stderr=True, stream=True, follow=True)
            for line in output:
                print(line.decode("utf-8"), end="", flush=True)

            return_code = container.wait()["StatusCode"]
        finally:
            if container is not None:
                container.remove(force=True)

    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, "")


def _set_property(args, arg, key):
    if getattr(args, arg) is not None:
        configuration.set_property(key, str(getattr(args, arg)))


def _job_run(args):
    _set_property(args, "cores", "ignis.executor.cores")
    _set_property(args, "instances", "ignis.executor.instances")
    _set_property(args, "mem", "ignis.executor.memory")
    _set_property(args, "gpu", "ignis.executor.gpu")
    _set_property(args, "img", "ignis.driver.image")
    _set_property(args, "img", "ignis.executor.image")
    _set_property(args, "driver_cores", "ignis.driver.cores")
    _set_property(args, "driver_mem", "ignis.driver.memory")
    _set_property(args, "driver_img", "ignis.driver.image")

    for entry in args.property:
        configuration.set_property(*entry.split("=", 1))

    job = ["run"]

    if args.name is not None:
        job.append(args.name)

    for entry in args.env:
        job += ["--env", entry]

    for entry in args.bind:
        job += ["--bind", entry]

    if args.interactive:
        job.append("--interactive")

    if args.time is not None:
        job += ["--time", args.time]

    if args.static is not None:
        job += ["--static", args.static]
        if args.static != "-" and os.path.exists(args.static):
            file = os.path.abspath(args.static)
            configuration.set_property(f"ignis.submitter.binds.{file}", file)

    if args.verbose:
        job.append("--verbose")

    if args.debug:
        job.append("--debug")

    job.append(args.command)

    if not configuration.get_bool("ignis.container.hostpipe") and \
            configuration.get_string("ignis.container.provider") == "docker":
        return _container_job(job + args.args, args.interactive)

    with tempfile.TemporaryDirectory() as tmp:
        configuration.set_property(f"ignis.submitter.binds./ignis-pipes", tmp)
        files = [os.path.join(tmp, f) for f in ["run", "code", "out", "err", "script"]]

        os.mkfifo(files[0], mode=0o600)
        os.mkfifo(files[1], mode=0o600)

        def run_pipe():
            with open(files[0]) as fifo:
                while True:
                    run = fifo.readline()
                    if run == "run\n":
                        with open(files[2], "w") as out, open(files[3], "w") as err:
                            code = subprocess.run(args=["bash", files[4]], stdout=out, stderr=err).returncode
                        with open(files[1], "w") as file:
                            file.write(str(code))

        pipe_proc = Process(target=run_pipe, name="ignis-pipe")
        try:
            pipe_proc.start()
            _container_job(job + args.args, args.interactive)
        finally:
            pipe_proc.kill()


def _list(args):
    _container_job(["list"], False)


def _info(args):
    cmd = ["info", args.id]
    if args.field is not None:
        cmd.extend(["--field", args.field])
    _container_job(cmd, False)


def _cancel(args):
    _container_job(["cancel", args.id], False)
