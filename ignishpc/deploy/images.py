import datetime
import fnmatch
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from distutils.version import StrictVersion

import docker
import docker.errors

try:
    import git

    GIT_ERROR = None
except Exception as ex:
    GIT_ERROR = ex

MODULE_NAME = "images"


def clear(yes, version, whitelist, blacklist, add_none, force, default_registry, namespace):
    client = docker.from_env()
    images = __getImages(client, version, default_registry, namespace, whitelist, blacklist, none=add_none)
    images.sort(key=lambda x: x[2], reverse=True)

    print("Following images will be cleared:")
    print("IMAGE ID       CREATED          TAG")
    now = datetime.datetime.now()
    for img_id, img_tag, created in images:
        print(img_id[7:19], " ", __dateFormat(now - created), img_tag if img_tag is not None else "<none>")
    if not yes:
        option = input("Are you sure (yes/no): ")
        while option not in ["yes", "no"]:
            option = input("Please type yes/no: ")
        yes = option == "yes"
    if yes:
        for img_id, img_tag, _ in images:
            try:
                client.images.remove(image=img_tag if img_tag is not None else img_id, force=force)
            except docker.errors.APIError as ex:
                print(img_id[7:19], "can't be removed:", ex.explanation)
    else:
        print("Aborted")


def push(yes, builders, version, whitelist, blacklist, default_registry, namespace):
    client = docker.from_env()
    images = __getImages(client, version, default_registry, namespace, whitelist, blacklist)
    images.sort(key=lambda x: x[2])
    if not builders:
        builder = re.compile(".*\/.*-?builder(:.+)?")
        images = list(filter(lambda img: not builder.match(img[1]), images))

    print("Following images will be pushed:")
    print("IMAGE ID       CREATED          TAG")
    now = datetime.datetime.now()
    for img_id, img_tag, created in images:
        print(img_id[7:19], " ", __dateFormat(now - created), img_tag if img_tag is not None else "<none>")
    if not yes:
        option = input("Are you sure (yes/no): ")
        while option not in ["yes", "no"]:
            option = input("Please type yes/no: ")
        yes = option == "yes"
    if yes:
        for _, img_tag, _ in images:
            client = docker.from_env()
            for line in client.images.push(img_tag, stream=True, decode=True):
                if 'errorDetail' in line:
                    print("Aborting")
                    raise docker.errors.APIError(line['errorDetail']['message'])
            print(img_tag, "PUSHED")


def build(sources, local_sources, ignore_folders, version_filters, custom_images, bases, full, save_logs, version_tags,
          version, default_registry, namespace, platform):
    with tempfile.TemporaryDirectory(prefix="ignis") as wd:
        core_list = list()
        version_map = dict()
        for vf in version_filters:
            version_map[vf[0]] = vf[1]

        tmp = os.path.join(wd, "tmp")

        def ignore_hidden(path, names):
            hidden = []
            for name in names:
                if name.startswith("."):
                    hidden.append(name)
            return set(hidden)

        def prepare_sources(core_name, local, folder, sid):
            dockerfiles = os.path.join(folder, "Dockerfiles")
            if not os.path.exists(dockerfiles):
                print("warn: " + folder + " ignored, Dockerfiles folder not found")
                return
            subfolders = list(filter(lambda name: name not in ignore_folders, os.listdir(dockerfiles)))
            core_folder = os.path.join(wd, core_name, str(sid))
            if local:
                shutil.copytree(folder, core_folder, dirs_exist_ok=True, ignore=ignore_hidden)
            else:
                shutil.move(folder, core_folder)
            v = __setVersion(core_name, core_folder, version_map.get(core_name, version))

            git_folder = os.path.join(core_folder, ".git")
            if os.path.exists(git_folder):
                shutil.rmtree(git_folder, ignore_errors=True)

            for core in subfolders:
                core_list.append((core, core_folder, v))
            print("  " + core_name + ":" + v)

        print("Sources:")
        sid = 0
        if len(sources) > 0 and GIT_ERROR is not None:
            raise GIT_ERROR
        for src in sources:
            name = src.split("/")[-1]
            if name.endswith(".git"):
                name = name[:-len(".git")]
            git.Repo.clone_from(src, tmp)
            prepare_sources(name, False, tmp, sid)
            sid += 1

        for src in local_sources:
            name = os.path.basename(src[:-1] if src[-1] == '/' else src)
            prepare_sources(name, True, src, sid)
            sid += 1

        print("Dockerfiles:")
        duplicates = set()
        build_list = list()
        libs = dict()
        for name, path, v in core_list:
            folder = os.path.join(path, "Dockerfiles", name)
            dfiles = __find(folder, "Dockerfile")
            for dfile in dfiles:
                order_file = os.path.join(os.path.dirname(dfile), "order")
                id = os.path.relpath(os.path.dirname(dfile), os.path.join(path, "Dockerfiles")).replace("/", "-")
                if os.path.exists(order_file):
                    with open(order_file) as f:
                        order = int(f.readline())
                elif id.endswith("-builder"):
                    order = 50
                elif id.endswith("-lib"):
                    order = 200
                    core = id.split('-')[0]
                    if core in libs:
                        libs[core].append(id)
                    else:
                        libs[core] = [id]
                    id += "-builder"
                else:
                    order = 100

                if id in duplicates:
                    raise RuntimeError(id + " is already defined")
                duplicates.add(id)

                build_list.append({
                    "id": id,
                    "name": default_registry + namespace + id,
                    "path": path,
                    "dockerfile": dfile,
                    "log": os.path.join(os.path.dirname(dfile), "build.log"),
                    "version": v,
                    "order": order,
                })
                print("  " + os.path.relpath(build_list[-1]["dockerfile"], build_list[-1]["path"]))

        real_cores = dict()
        print("Cores:")
        cores_version = dict()
        for core in build_list[:]:
            if core["id"].endswith("-builder") and not core["id"].endswith("lib-builder"):
                id = core["id"][0: -len("-builder")]
                cores_version[id] = core["version"]
                real_cores[id] = core
                print("  " + id)
                if core["id"] in ("driver-builder", "executor-builder") or \
                        (not bases and core["id"] == "common-builder"):
                    continue
                c_libs = libs.get(id, list())
                build_list.append(
                    __createDockerfile(wd, id + "-driver", ["driver", id] + c_libs, core["version"], default_registry,
                                       namespace, 300))
                build_list.append(
                    __createDockerfile(wd, id + "-executor", ["executor", id] + c_libs, core["version"],
                                       default_registry, namespace, 300))
                build_list.append(
                    __createDockerfile(wd, id if id != "common" else "common-full", ["driver", "executor", id] + c_libs,
                                       core["version"], default_registry, namespace, 301))
        full_libs = list()
        if len(libs) > 0:
            ignore_libs = list()
            print("Libraries:")
            for core, names in libs.items():
                for name in names:
                    init = name.index(core) + len(core) + 1
                    end = name.index("-lib")
                    print("  " + name[init:end] + " (" + core + ")", end="")
                    if core in real_cores:
                        full_libs.append(name)
                        print()
                    else:
                        ignore_libs.append(name + "-builder")
                        print(" IGNORED")

                if core not in real_cores:
                    continue

                build_list.append(
                    __createDockerfile(wd, core + "-libs-compiler", names, cores_version[core], default_registry,
                                       namespace, 300, base=names[0] + "-builder"))
            build_list = list(filter(lambda d: d["id"] not in ignore_libs, build_list))

        if real_cores and full:
            custom_images.insert(0, ["full", "driver", "executor"] + list(real_cores.keys()) + full_libs)

        i = 301
        for img in custom_images:
            i += 1
            if version:
                custom_version = version
            elif "common" in real_cores:
                custom_version = real_cores["common"]["version"]
            else:
                custom_version = "latest"
            build_list.append(
                __createDockerfile(wd, img[0], img[1:], custom_version, default_registry, namespace, i))

        build_list.sort(key=lambda x: x["order"])

        print("Images:")
        order = None
        for build in build_list:
            if build["order"] != order:
                print("  ---(" + str(build["order"]) + ")---")
            order = build["order"]
            print("  " + build["name"] + ":" + build["version"])

        build_list.append({
            "order": None
        })

        print("Build:")
        image_list = list()
        with ThreadPoolExecutor() as executor:
            wait_list = list()
            order = build_list[0]["order"]
            for build in build_list:
                if build["order"] != order:
                    error = None
                    for info, wait in wait_list:
                        log = os.path.join(os.getcwd(), "ignisbuild-" + info["id"] + ".log")
                        try:
                            print("  " + info["name"] + ":" + info["version"], end=" ", flush=True)
                            image_list.append((info, wait.result()))
                            print("SUCCESS")
                            if save_logs:
                                shutil.copy(info["log"], log)
                        except Exception as ex:
                            print("FAILED, check " + log)
                            shutil.copy(info["log"], log)
                            error = ex
                    if error:
                        print("Aborting")
                        raise error
                    wait_list.clear()
                order = build["order"]
                if order is None:
                    break
                wait_list.append((build, executor.submit(
                    __docker_build,
                    name=build["name"],
                    path=build["path"],
                    dockerfile=build["dockerfile"],
                    log=build["log"],
                    version=build["version"],
                    default_registry=default_registry,
                    namespace=namespace,
                    platform=platform
                )))
        print("Build end")
        if version_tags:
            print("Setting additional version tag:")
            for vt in version_tags:
                for info, img in image_list:
                    tag = info['name'] + ':' + vt
                    img.tag(tag)
                    print("  ", tag)


def singularity(name, output, host, default_registry, platform, force):
    client = docker.from_env()
    img_id = default_registry + name
    if ':' not in name:
        img_id += ":latest"
    imgs = client.images.list(name=img_id, filters={"label": ["ignis"]})
    if len(imgs) == 0:
        raise RuntimeError("error: " + img_id + " not found")

    resources = os.path.join(os.path.dirname(os.path.realpath(__file__)), "resources", "singularityce")
    with tempfile.NamedTemporaryFile(mode="wb") as image:
        for chunk in imgs[0].save(named=False):
            image.write(chunk)
        cmd = ["singularity", "build"]
        if force:
            cmd.append("--force")

        if host:
            import subprocess
            with tempfile.NamedTemporaryFile(mode='w', suffix=".def") as sin:
                with open(os.path.join(resources, "build.def")) as tp:
                    sin.write(tp.read().replace("/ignis.img", image.name))
                    sin.flush()

                process = subprocess.Popen(cmd + [output, sin.name],
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
                (err, _) = process.communicate()
                exit_code = process.wait()
        else:
            output_folder = os.path.dirname(output)
            output_file = os.path.basename(output)
            mounts_list = [
                docker.types.Mount(source=image.name, target="/ignis.img", type="bind"),
                docker.types.Mount(source=os.path.join(resources, "build.def"), target="/build.def", type="bind"),
                docker.types.Mount(source=os.path.abspath(output_folder), target="/target", type="bind"),
            ]
            namespace = img_id[:img_id.rindex('/')]
            try:
                container = client.containers.run(
                    image=namespace + "/singularityce",
                    remove=True,
                    platform=platform,
                    privileged=True,
                    command=cmd + ["/target/" + output_file, "/build.def"],
                    mounts=mounts_list,
                )
                exit_code = 0
                err = ""
            except docker.errors.ContainerError as ex:
                exit_code = ex.exit_status
                err = ex.stderr
    if exit_code != 0:
        raise RuntimeError("singularity fails with error " + str(exit_code) + "\n" + err)


def __getImages(client, version, default_registry, namespace, whitelist, blacklist, none=False):
    labels = ["ignis"] if version is None else ["ignis=" + version]
    prefix = default_registry + namespace
    imgs = client.images.list(name=prefix + "*", filters={"label": labels})
    ref_names = list()
    white_tags = set()
    black_tags = set()
    result = list()
    wildcard_s = re.compile(r"[\*\?\[\]\!#]")

    for img in imgs:
        for tag in img.attrs['RepoTags']:
            if "/" in tag:
                tag = tag.split("/")[-1]
            if ":" in tag:
                ref_names.append(tag.split(":")[0])
            else:
                ref_names.append(tag)

    def prepare_tag(lst, tags):
        if lst is not None:
            lst2 = list()
            for name in lst:
                if wildcard_s.search(name):
                    lst2.extend(fnmatch.filter(ref_names, name))
                else:
                    lst2.append(name)
            lst = lst2
            for name in lst:
                if ":" in name:
                    tags.add(name)
                else:
                    tags.add(name + ":latest")

    prepare_tag(whitelist, white_tags)
    prepare_tag(blacklist, black_tags)

    for img in imgs:
        for tag in img.tags:
            if tag.startswith(prefix):
                name = tag[len(prefix):]
                if whitelist is not None and name not in white_tags:
                    continue
                if name in black_tags:
                    continue
                result.append((img.id, tag, __getDate(img)))

    if none:
        imgs = client.images.list(filters={"label": labels})
        root_nones = list(filter(lambda img: len(img.attrs['RepoTags']) == 0, imgs))
        layers = client.images.list(filters={"label": labels}, all=True)
        layer_map = dict()
        for layer in layers:
            layer_map[layer.id] = layer
        nones = list()
        while len(root_nones) > 0:
            none = root_nones.pop()
            nones.append(none)
            parent_id = none.attrs['Parent']
            if not parent_id or parent_id not in layer_map:
                continue
            parent = layer_map[parent_id]
            if len(parent.attrs['RepoTags']) == 0:
                root_nones.append(parent)

        for img in nones:
            if len(img.tags) > 0:
                for tag in img.tags:
                    result.append((img.id, tag))
            else:
                result.append((img.id, None, __getDate(img)))

    return result


def __getDate(img):
    sdate = img.attrs['Created']
    nano = sdate.split(".")[-1]
    sdate = sdate[0:-len(nano)] + nano[0:6]
    if sdate[-1] != 'Z':
        sdate += 'Z'
    return datetime.datetime.strptime(sdate, '%Y-%m-%dT%H:%M:%S.%fZ')


def __dateFormat(elapsed):
    seconds = int(elapsed.total_seconds())
    periods = [
        ('year', 60 * 60 * 24 * 365),
        ('month', 60 * 60 * 24 * 30),
        ('day', 60 * 60 * 24),
        ('hour', 60 * 60),
        ('minute', 60),
        ('second', 1)
    ]
    for pname, pseconds in periods:
        if seconds > pseconds:
            value = int(seconds / pseconds)
            result = "{} {}{} {}".format(value, pname, 's' if value > 1 else '', "ago")
            result += " " * (16 - len(result))
            return result


def __is_git(path):
    if GIT_ERROR is not None:
        return False
    try:
        _ = git.Repo(path).git_dir
        return True
    except git.exc.InvalidGitRepositoryError:
        return False


def __find(path, name):
    result = []
    for root, dirs, files in os.walk(path):
        if name in files:
            result.append(os.path.join(root, name))
    return result


def __setVersion(name, path, version):
    if version is None:
        return "latest"
    if not __is_git(path):
        return version
    repo = git.Repo(path)
    tags = sorted(repo.tags, key=StrictVersion)
    found = False
    for tag in tags:
        if tag.startswith(version):
            found = True
            break
    if not found:
        raise ValueError("error: " + name + " has not version " + version)
    repo.heads[tag].checkout()
    return tag


def __docker_build(name, path, dockerfile, log, version, default_registry, namespace, platform):
    error = None
    try:
        if platform is None:
            client = docker.from_env()
            build2 = client.images.build
        else:
            build2 = __buildx
        imageObj, buildlog = build2(
            path=path,
            dockerfile=dockerfile,
            labels={
                "ignis": version
            },
            tag=name + ":" + version,
            buildargs={
                "REGISTRY": default_registry,
                "NAMESPACE": namespace,
                "TAG": ":" + version,
                "RELPATH": os.path.relpath(os.path.dirname(dockerfile), path) + "/"
            },
            platform=platform
        )
    except docker.errors.BuildError as ex:
        imageObj = None
        buildlog = ex.build_log
        manifest_error = re.compile(".*manifest for (.*) not found.*")
        if type(ex.msg) == dict:
            if "message" in ex.msg:
                ex.msg = ex.msg["message"]
            else:
                ex.msg = str(ex.msg)
        result = manifest_error.search(ex.msg)
        if result:
            ex.msg = result.group(1) + " required, use --sources or --local-source to add Dockerfile"
        error = RuntimeError(ex.msg)
    except Exception as ex:
        imageObj = None
        buildlog = []
        error = ex

    # Remove ANSI color codes from the string.
    strip = re.compile('\033\\[([0-9]+)(;[0-9]+)*m')
    with open(log, "w") as file:
        for raw in buildlog:
            if 'stream' in raw:
                file.write(re.sub(strip, '', raw['stream']))
    if error:
        raise error

    return imageObj


def __createDockerfile(wd, id, cores, version, default_registry, namespace, order=100, base="common"):
    cores = list(sorted(set(cores)))
    driver = False
    executor = False
    if "common" in cores:
        cores.remove("common")
    if "executor" in cores:
        executor = True
        cores.remove("executor")
    if "driver" in cores:
        driver = True
        cores.remove("driver")

    path = os.path.join(wd, id + "-Dockerfile")
    os.mkdir(path)
    dfile = os.path.join(wd, os.path.join(path, "Dockerfile"))
    with open(dfile, "w") as file:
        file.write("""
        ARG REGISTRY=""
        ARG NAMESPACE="ignishpc/"
        ARG TAG=""
        FROM ${REGISTRY}${NAMESPACE}""" + base + """${TAG}
        ARG RELPATH=""
        """)
        for core in cores:
            builder = default_registry + namespace + core + "-builder:" + version
            file.write("COPY --from=" + builder + " ${IGNIS_HOME} ${IGNIS_HOME}\n")
            file.write("RUN 	${IGNIS_HOME}/bin/ignis-" + core + "-install.sh && ")
            file.write("rm -f ${IGNIS_HOME}/bin/ignis-" + core + "-install.sh\n")

        if driver:
            file.write("RUN ${IGNIS_HOME}/common/driver-install.sh\n")

        if executor:
            file.write("RUN ${IGNIS_HOME}/common/executor-install.sh\n")

    return {
        "id": id,
        "name": default_registry + namespace + id,
        "path": path,
        "dockerfile": dfile,
        "log": os.path.join(path, "build.log"),
        "version": version,
        "order": order,
    }


def __buildx(path, dockerfile, labels, tag, buildargs, platform):
    import subprocess

    def join(name, values):
        array = list()
        for value in values:
            array.append(name)
            array.append(value)
        return array

    process = subprocess.Popen(["docker", "buildx", "build",
                                "--progress", "plain",
                                "--load",
                                "--file", dockerfile,
                                "--tag", tag,
                                "--platform", platform,
                                ] +
                               join("--build-arg", [key + "=" + value for key, value in buildargs.items()]) +
                               join("--label", [key + "=" + value for key, value in labels.items()]) +
                               [path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
    (output, _) = process.communicate()
    exit_code = process.wait()
    buildlog = [{'stream': output}]
    if exit_code != 0:
        lines = output.split("\n")
        reason = lines[-2] if len(lines) > 1 else output
        if "does not exist" in reason or "not found" in reason:
            for line in reversed(lines):
                if "load metadata for" in line:
                    reason = "manifest for " + line.split(" ")[-1][:-1] + " not found"
                    break
        raise docker.errors.BuildError(reason, buildlog)

    client = docker.from_env()
    return client.images.get(tag), buildlog
