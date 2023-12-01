import os
import subprocess
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
        con = c.attach_socket(params={'stdin': True, 'stream': True})
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


def _container_job(args, wdir, files, it):
    it = False
    writable = configuration.get_property("ignis.container.writable")
    network = configuration.network()
    if configuration.get_property("ignis.container.provider") == "singularity":
        cmd = ["singularity", "exec", "--cleanenv"]

        if wdir is not None:
            cmd.extend(["--workdir", wdir])

        if writable:
            cmd.append("--writable-tmpfs")

        if network != "default":
            cmd.extend(["--net", "--network", network])

        for file in files:
            cmd.extend(["--bind", file])

        proc = subprocess.Popen(
            args=cmd + [configuration.default_image(), "bash", "-c", "ignis-submit"] + args,
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
        root = configuration.get_property("ignis.container.docker.root")
        other_args = {}

        if network in ("host", "bridge", "none"):
            other_args["network_mode"] = network
        elif network != "default":
            other_args["network"] = network

        if wdir is not None:
            other_args["working_dir"] = wdir

        container = None
        try:
            container = docker.from_env().containers.create(
                image=configuration.default_image(),
                command=["bash", "-c", "ignis-submit"] + args,
                environment={},
                mounts=[docker.types.Mount(f, f, type="bind") for f in files],
                read_only=not writable,
                user="root" if root else "{}:{}".format(os.getuid(), os.getgid()),
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
        configuration.set_property(key, getattr(args, arg))


def _job_run(args):
    _set_property(args, "cores", "ignis.executor.cores")
    _set_property(args, "instances", "ignis.executor.instances")
    _set_property(args, "mem", "ignis.executor.memory")
    _set_property(args, "gpu", "ignis.executor.gpu")
    _set_property(args, "img", "ignis.executor.image")
    _set_property(args, "driver_cores", "ignis.driver.cores")
    _set_property(args, "driver_mem", "ignis.driver.memory")
    _set_property(args, "driver_img", "ignis.driver.image")

    for entry in args.property:
        if "=" not in entry:
            raise RuntimeError("Bad property variable")
        configuration.set_property(*entry.split("=", 1))

    buffer = io.BytesIO()
    configuration.yaml.dump(configuration.props, buffer)

    env_props = base64.b64encode(buffer.getvalue()).decode("utf-8")
    wdir = os.path.expanduser(configuration.get_property("ignis.wdir"))

    job = ["run"]
    files = [os.path.abspath(wdir)]

    if args.name is not None:
        job.append(args.name)

    for entry in args.env:
        job += ["--env", entry]

    job += ["--env", env_props]

    if args.interactive:
        job.append("--interactive")

    if args.time is not None:
        job += ["--time", args.time]

    if args.static is not None:
        job += ["--static", args.static]

    if args.scheduler_args is not None:
        job += ["--scheduler-args", args.scheduler_args]
        if args.scheduler_args != '-' and os.path.exists(args.scheduler_args):
            files.append(os.path.abspath(args.scheduler_args))

    job.append(args.command)

    _container_job(job + args.args, wdir, files, args.interactive)


def _list(args):
    _container_job(["list"], os.path.expanduser(configuration.get_property("ignis.wdir")), [], False)


def _info(args):
    _container_job(["info", args.id], os.path.expanduser(configuration.get_property("ignis.wdir")), [], False)


def _cancel(args):
    _container_job(["cancel", args.id], os.path.expanduser(configuration.get_property("ignis.wdir")), [], False)
