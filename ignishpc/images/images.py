import os

import docker
import docker.errors
import docker.types
import subprocess
import fnmatch
import datetime
import tempfile

from ignishpc.common import configuration
from ignishpc.images import build


def _run(args):
    return {
        "build": build._run,
        "list": _list,
        "rm": _rm,
        "push": _push,
        "pull": _pull
    }[args.action](args)


def _get_images(patterns, untagged=False):
    client = docker.from_env()

    ignis_images = client.images.list(filters={"label": ["ignis.version"]})
    images = list()

    for entry in ignis_images:
        has_pattern = any([any([fnmatch.fnmatch(tag, pat) for tag in entry.tags]) for pat in patterns])

        if (len(entry.tags) > 0 and (len(patterns) == 0 or has_pattern)) or (len(entry.tags) == 0 and untagged):
            images.append(entry)

    return images


def _print_images(images):
    table = list()
    for img in images:
        table.append((img.short_id.split(":")[1],
                      _image_date(img),
                      img.attrs["Architecture"] if "Architecture" in img.attrs else "",
                      " ".join(img.tags) if len(img.tags) > 0 else "<none>"))

        now = datetime.datetime.now()
        table = sorted(table, key=lambda row: now - (row[1] if row[1] is not None else datetime.datetime.min),
                       reverse=True)

    print("IMAGE ID       CREATED        ARCH     TAG")
    for img_id, created, arch, tag in table:
        print(img_id.ljust(14), (_date_format(now - created) if created is not None else "").ljust(14),
              arch.ljust(8),
              tag)


def _ask_before(args):
    if not args.yes:
        option = input("Are you sure (yes/no): ")
        while option not in ["yes", "no"]:
            option = input("Please type yes/no: ")
        return option == "yes"
    return args.yes


def _list(args):
    images = _get_images(args.pattern, args.untagged)
    _print_images(images)


def _rm(args):
    images = _get_images(args.pattern, args.untagged)
    print("Following images will be deleted:")
    _print_images(images)

    if _ask_before(args):
        to_remove = list()
        for img in images:
            created = _image_date(img)
            created = created if created is not None else datetime.datetime.min
            if len(img.tags) > 0:
                tags = [tag for tag in img.tags if any([fnmatch.fnmatch(tag, pat) for pat in args.pattern])]
                for tag in tags:
                    to_remove.append((created, tag))
            else:
                to_remove.append((created, img.short_id))
        client = docker.from_env()

        for _, tag in sorted(to_remove, key=lambda t: t[0], reverse=True):
            try:
                client.images.remove(image=tag, force=args.force)
            except docker.errors.APIError as ex:
                print(tag, "can't be removed:", ex.explanation)


def _push(args):
    images = _get_images(args.pattern, False)
    print("Following images will be pushed:")
    _print_images(images)

    if _ask_before(args):
        client = docker.from_env()
        for img in images:
            for tag in img.tags:
                print(tag, end="...", flush=True)
                for line in client.images.push(tag, stream=True, decode=True):
                    if 'errorDetail' in line:
                        print("ERROR")
                        raise RuntimeError(line['errorDetail']['message'])
                print("PUSHED")


def _pull(args):
    provider = configuration.get_string("ignis.container.provider")
    if args.singularity is not None and provider != "docker":
        source = "docker-daemon:" if args.local else "docker://"
        image = args.image
        if ":" not in image:
            image += ":latest"
        print("pulling image")
        subprocess.run(args=[provider, "pull", "--force", args.singularity, source + image])
        return

    client = docker.from_env()
    if args.local and args.singularity is not None:
        image = client.images.get(args.image)
    else:
        print("pulling image")
        image = client.images.pull(args.image)
        print("pull complete")
        if args.singularity is None:
            return
    target = os.path.abspath(args.singularity)

    with tempfile.TemporaryDirectory(prefix="ignis-build-") as wd:
        source = os.path.abspath(os.path.join(wd, "ignis.image"))
        print("writing to disk")
        with open(source, "wb") as file:
            for chunk in image.save():
                file.write(chunk)
            file.flush()

        print("converting image to sif format")
        try:
            client.containers.run(
                image=configuration.format_image("singularity"),
                command=["sh", "-c", f"singularity pull --force {os.path.basename(target)} docker-archive:///source && "
                                     f"chown {os.getuid()}:{os.getgid()} {os.path.basename(target)}"],
                remove=True,
                mounts=[docker.types.Mount("/source", source, "bind"),
                        docker.types.Mount("/target", os.path.dirname(target), "bind")],
                platform=args.arch,
                working_dir="/target",
                stdout=True,
                stderr=True,
            )

        except docker.errors.ContainerError as ex:
            raise RuntimeError(ex.stderr)
        print("image saved in " + args.singularity)


def _image_date(img):
    if 'Created' in img.attrs:
        sdate = img.attrs['Created']
        nano = sdate.split(".")[-1]
        sdate = sdate[0:-len(nano)] + nano[0:6]
        if sdate[-1] != 'Z':
            sdate += 'Z'
        return datetime.datetime.strptime(sdate, '%Y-%m-%dT%H:%M:%S.%fZ')
    return None


def _date_format(elapsed):
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
            return "{} {}{} ago".format(value, pname, 's' if value > 1 else '')
