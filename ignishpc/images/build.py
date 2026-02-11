import os
import re
import tempfile
import shutil
import fnmatch
import shlex
import textwrap
import subprocess
from collections import namedtuple

import git
import docker
import docker.errors

from ignishpc.common import configuration


def _replace_all(s, vars):
    for var, value in vars.items():
        s = s.replace("${" + var + "}", value).replace("$" + var, value)
    return s


def _rmdup(l):
    return list(dict.fromkeys(l))


def _folder_gen(wd):
    i = 0
    while True:
        yield os.path.join(wd, str(i))
        i += 1


def _ignore_hidden(path, names):
    hidden = []
    for name in names:
        if name.startswith("."):
            hidden.append(name)
    return set(hidden)


def _dump_log(buildlog, path, msg=None):
    # Remove ANSI color codes from the string.
    strip = re.compile('\033\\[([0-9]+)(;[0-9]+)*m')
    with open(path, "w") as file:
        for raw in buildlog:
            if isinstance(raw, dict) and 'stream' in raw:
                file.write(re.sub(strip, '', raw['stream']))
            elif isinstance(raw, str):
                file.write(re.sub(strip, '', raw))
        if msg is not None:
            file.write(msg)


def _parse_dockerfile(folder, subpath, name):
    lines = list()
    path = os.path.join(folder, subpath, "Dockerfile")
    with open(path) as file:
        newline = True
        for line in file:
            scape = len(line) > 1 and line[-2] == '\\'
            if scape:
                line = line[:-2]
            line = line.strip()

            if newline:
                lines.append(line)
            else:
                lines[-1] += " " + line
            newline = not scape
    requires = set()
    args = set()
    labels = {}
    for line in lines:
        if any(line.upper().startswith(prefix) for prefix in ["FROM", "LABEL", "ARG", "COPY"]):
            fields = shlex.split(line, posix=False)[1:]
            try:
                if line[0].upper() == "F":
                    if fields[0].startswith("--"):
                        fields = fields[1:]
                    requires.add(fields[0])
                elif line[0].upper() == "L":
                    for label in fields:
                        key, value = label.split("=")
                        labels[key] = value[1:-1]
                elif line[0].upper() == "A":
                    tag = fields[0] if '=' not in fields[0] else fields[0].split("=")[0]
                    args.add(tag)
                else:
                    for field in fields:
                        if field.startswith("--from"):
                            requires.add(fields[0].split("=")[1])
                            break
            except Exception as ex:
                print("warn: " + line + " is ignored by parser, " + str(ex))

    return namedtuple("Dockerfile", "folder, path, subpath, name requires args labels"
                      )(folder, path, subpath, name, requires, args, labels)


def _create_dockerfile(path, name, cores, core_libs, build_args):
    header = """
    ARG REGISTRY=""
    ARG NAMESPACE="ignishpc/"
    ARG TAG=""
    FROM ${REGISTRY}${NAMESPACE}template${TAG}
    """

    builder = """
    COPY --from=${REGISTRY}${NAMESPACE}${CORE}-builder${TAG} ${IGNIS_HOME} ${IGNIS_HOME}
    RUN ${IGNIS_HOME}/bin/ignis-${CORE}-install.sh && \\
        rm -f ${IGNIS_HOME}/bin/ignis-${CORE}-install.sh
    """

    lib_builder = builder.replace("-builder", "-lib")

    dockerfile = ""
    for core in cores:
        dockerfile += builder.replace("${CORE}", core)
        for lib in core_libs.get(core, []):
            dockerfile += lib_builder.replace("${CORE}", lib)
    for arg, value in build_args.items():
        dockerfile = dockerfile.replace("${" + arg + "}", value)

    dockerfile = textwrap.dedent(header + dockerfile)
    folder = os.path.join(path, "Dockerfiles")
    os.makedirs(folder)
    with open(os.path.join(folder, "Dockerfile"), "w") as file:
        file.write(dockerfile)
    return _parse_dockerfile(folder, "", name)


def _build(name, path, dockerfile, build_args, labels, arch, logfile, debug):
    try:
        client = docker.from_env()

        image, buildlog = client.images.build(
            path=path,
            tag=name,
            dockerfile=dockerfile,
            labels=labels,
            platform=arch,
            buildargs=build_args
        )
        if debug:
            _dump_log(buildlog, logfile)
    except docker.errors.BuildError as ex:
        manifest_error = re.compile(".*manifest for (.*) not found.*")
        msg = ex.msg
        if isinstance(msg, dict):
            if "message" in msg:
                msg = msg["message"]
            else:
                msg = str(msg)
        result = manifest_error.search(msg)
        if result:
            msg += "\n" + result.group(1) + " required, use -s/--sources to add the Dockerfile"
        _dump_log(ex.build_log, logfile, msg=msg)
        return False

    return True


def _buildx(name, path, dockerfile, build_args, labels, arch, logfile, debug):
    result = subprocess.run(["docker", "buildx", "version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(result.stdout.decode("utf-8"))

    result = subprocess.run(["docker", "buildx", "inspect", "ignishpc"], capture_output=True)
    if result.returncode != 0:
        result = subprocess.run(["docker", "buildx", "create", "--name", "ignishpc"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            raise RuntimeError(result.stdout.decode("utf-8"))

    raw_build_args = sum([["--build-arg", arg + "=" + val] for arg, val in build_args.items()], [])
    raw_labels = sum([["--label", lab + "=" + val] for lab, val in labels.items()], [])

    result = subprocess.run(["docker", "buildx", "build",
                             "--builder", "ignishpc",
                             "--file", dockerfile,
                             "--no-cache",
                             "--platform", arch,
                             "--progress", "plain",
                             "--push",
                             "--tag", name,
                             "."] + raw_build_args + raw_labels,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=path)

    if debug or result.returncode != 0:
        _dump_log([{'stream': result.stdout.decode("utf-8")}], logfile)

    return result.returncode == 0


def _run(args):
    build_args = {
        "REGISTRY": args.registry,
        "NAMESPACE": args.namespace,
        "TAG": args.tag,
        "BUILD_CORES": str(os.cpu_count()) if args.jobs is None else args.jobs,
        "VERSION": "dev" if args.tag == "latest" else args.tag
    }

    if build_args["REGISTRY"] is None:
        build_args["REGISTRY"] = configuration.get_string("ignis.container.docker.registry")

    if build_args["NAMESPACE"] is None:
        build_args["NAMESPACE"] = configuration.get_string("ignis.container.docker.namespace")

    if len(build_args["REGISTRY"]) != 0 and not build_args["REGISTRY"].endswith("/"):
        build_args["REGISTRY"] += "/"
    if len(build_args["NAMESPACE"]) != 0 and not build_args["NAMESPACE"].endswith("/"):
        build_args["NAMESPACE"] += "/"
    if len(build_args["TAG"]) != 0 and build_args["TAG"][0] != ':':
        build_args["TAG"] = ":" + build_args["TAG"]

    with tempfile.TemporaryDirectory(prefix="ignis-build-") as wd:
        new_folder = _folder_gen(wd)
        print("Sources:")
        sources = list()

        for i, src in enumerate(args.sources):
            target = next(new_folder)
            os.mkdir(target)
            if "://" in src:
                field = src.split("@") if "@" in src else src.split()
                if len(field) == 2:
                    git.Repo.clone_from(field[0], target, branch=field[1])
                else:
                    git.Repo.clone_from(field[0], target)
                shutil.rmtree(os.path.join(target, ".git"), ignore_errors=True)
            else:
                shutil.copytree(src, target, dirs_exist_ok=True, ignore=_ignore_hidden)
            if "Dockerfiles" in os.listdir(target):
                print(" ", src)
                sources.append(target)
            else:
                shutil.rmtree(target, ignore_errors=True)

        print()
        print("Dockerfiles:")
        dockerfiles = {}
        for src in sources:
            folder = os.path.join(src, "Dockerfiles")
            for path, _, files in os.walk(folder):
                if "Dockerfile" in files:
                    subpath = os.path.relpath(path, folder)
                    name = subpath.replace("/", "-")
                    if name in dockerfiles:
                        raise RuntimeError(name + " is defined multiple times")
                    dockerfiles[name] = _parse_dockerfile(folder, subpath, name)

        for name, dockerfile in list(dockerfiles.items()):
            ignored = any([fnmatch.fnmatch(name, pat) for pat in args.ignore])
            optional = dockerfile.labels.get("ignis.build", False)

            if ignored or (optional and not args.all and not any([fnmatch.fnmatch(name, pat) for pat in args.enable])):
                print(" ", dockerfile.subpath, "#ignored")
                del dockerfiles[name]
            else:
                print(" ", dockerfile.subpath)

        get_cores = list()
        for core in args.get_cores:
            if not core.endswith("-builder") and not core.endswith("-lib"):
                get_cores.append(core + "-builder")
            else:
                get_cores.append(core)

        print()
        print("Cores:")
        cores = set()
        libs = dict()
        for name in list(dockerfiles.keys()) + get_cores:
            if name.endswith("-builder"):
                core_name = name[:-len("-builder")]
                cores.add(core_name)
                if "-" in core_name:
                    raise RuntimeError("core must not contain '-': " + core_name)
            elif name.endswith("-lib"):
                fields = name.split("-")[:-1]
                core = fields[0]
                libs_name = fields[1:]
                if len(libs_name) == 0:
                    raise RuntimeError("lib must have a name: " + name)
                if core not in libs:
                    cores.add(core)
                    libs[core] = list()
                prefix = core + "-"
                for lib in libs_name:
                    libs[core].append(prefix + lib)
                    prefix += lib + "-"
        if len(cores) > 0 and "base" not in cores:
            cores.add("base")
        cores = _rmdup(sorted(cores))
        for core in cores:
            if core + "-builder" in dockerfiles:
                print(" ", core)
            else:
                print(" ", core, "#no sources")

        print()
        print("Libs:")
        for core in libs:
            libs[core] = _rmdup(sorted(libs[core]))
            for lib in libs[core]:
                msg = " #no sources" if lib not in dockerfiles else ""
                print("  " + lib.split("-", maxsplit=1)[1] + " (" + core + ")" + msg)

        if args.core_images:
            for core in cores:
                dockerfiles[core] = _create_dockerfile(next(new_folder), core, _rmdup(["base", core]), libs, build_args)

        if args.name != '-':
            dockerfiles[args.name] = _create_dockerfile(next(new_folder), args.name, cores, libs, build_args)

        print()
        print("Images:")
        images = set()
        images_name = dict()
        for name, dockerfile in dockerfiles.items():
            image = build_args["REGISTRY"] + build_args["NAMESPACE"] + dockerfile.name + build_args["TAG"]
            images.add(image)
            images_name[name] = image
            print(" ", image)

        print()
        build = set()
        print("Build:")
        max_it = len(dockerfiles) + 1
        while len(build) != len(dockerfiles):
            max_it -= 1
            for name, dockerfile in dockerfiles.items():
                if images_name[name] in build:
                    continue
                if max_it == 0:
                    raise RuntimeError("dependency loop in" + name)
                local = dockerfile.labels.get("ignis.build.context", False)
                if not local:
                    dock_path = os.path.relpath(dockerfile.path, os.path.dirname(dockerfile.folder))
                    build_args["DOCK_DIR"] = os.path.dirname(dock_path) + "/"
                    build_args["RELPATH"] = build_args["DOCK_DIR"]  # legacy

                deps = [_replace_all(rawdep, build_args) for rawdep in dockerfile.requires]
                if any(dep in images and dep not in build and dep != images_name[name] for dep in deps):
                    continue

                print(" ", images_name[name], end="...", flush=True)
                logfile = dockerfile.name + ".log"

                if args.dry_run:
                    f = lambda *a, **k: True
                elif args.buildx:
                    f = _buildx
                else:
                    f = _build

                ok = f(name=images_name[name],
                       path=os.path.dirname(dockerfile.folder) if not local else os.path.dirname(dockerfile.path),
                       dockerfile=dockerfile.path if not local else os.path.basename(dockerfile.path),
                       build_args={key: val for key, val in build_args.items() if key in dockerfile.args},
                       labels={"ignis.version": build_args["VERSION"]},
                       arch=args.arch,
                       logfile=logfile,
                       debug=args.log)
                if ok:
                    print("OK")
                else:
                    print("ERROR", "->", logfile)
                    raise RuntimeError("Build abort")

                build.add(images_name[name])

        print("Build End")
