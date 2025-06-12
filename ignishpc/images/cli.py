from ignishpc.common.formatter import SmartFormatter, desc


def _cmd(args):
    from ignishpc.images.images import _run
    return _run(args)


def setup(subparsers):
    parser = subparsers.add_parser("images", **desc("Administrate Images (Docker v23.0+ Required)"),
                                   formatter_class=SmartFormatter,
                                   epilog="""Examples:
                                     | $ ignishpc images build -s URL
                                     | $ ignishpc images pull ignishpc -s ignishpc.sif""")

    actions = parser.add_subparsers(dest="action", title="Available Actions", metavar="<action>")
    actions.required = True

    build = actions.add_parser("build", **desc("Build images"), formatter_class=SmartFormatter,
                               epilog="""Examples:
                                     | $ ignishpc images build --buildx --arch linux/amd64,linux/arm64,linux/ppc64le
                                     | $ ignishpc images build -g coreA -g coreB --core-images --name -
                                     | $ ignishpc images build -a -s URL""")

    build.add_argument("-s", "--source", dest="sources", action="append", metavar="path/url",
                       help="repository URL or path. URL can specify a tag \"<URL> [tag]\"", default=[])
    build.add_argument("--name", action="store", metavar="str", default="ignishpc",
                       help="set a name for the final image, default 'ignishpc', '-' to disable")
    build.add_argument("-g", "--get-core", dest="get_cores", action="append", metavar="name",
                       help="ignore images that contains wildcard in name", default=[])
    build.add_argument("--core-images", action="store_true",
                       help="build isolated cores image", default=False)
    build.add_argument("-r", "--registry", action="store", metavar="str",
                       help="set image registry, default is the Docker Hub")
    build.add_argument("-n", "--namespace", action="store", metavar="str",
                       help="set image namespace, default 'ignishpc'")
    build.add_argument("-t", "--tag", action="store", metavar="str", default="latest",
                       help="set image tag, default 'latest'")
    build.add_argument("--log", action="store_true",
                       help="create a log of every build", default=False)
    build.add_argument("--arch", action="store",
                       help="specify the target architecture for building images")
    build.add_argument("-a", "--all", action="store_true",
                       help="build optional images")
    build.add_argument("-j", "--jobs", action="store", metavar="n", type=int,
                       help="try to set a limit of cores to build an image, default auto")
    build.add_argument("--ignore", action="store", metavar="folder", nargs="+",
                       help="ignore images that contains wildcard pattern in name", default=[])
    build.add_argument("--enable", action="store", metavar="folder", nargs="+",
                       help="enable optional images that contains wildcard pattern in name", default=[])
    build.add_argument("--dry-run", action="store_true", default=False,
                       help="perform a simulation of the build with checks but without creating any images")
    build.add_argument("--buildx", action="store_true", default=False,
                       help="perform multi-architecture building using Buildx. The result will be pushed and removed. "
                            "The docker binary binary must be available in PATH and the buildx plugin installed")

    _list = actions.add_parser("list", **desc("Display images"))
    _list.add_argument("-p", "--pattern", action="append", metavar="str", default=[],
                       help="filter images by wildcard pattern")
    _list.add_argument("-u", "--untagged", action="store_true",
                       help="display images without tags", default=False)

    rm = actions.add_parser("rm", **desc("Remove images"))
    rm.add_argument("-p", "--pattern", action="append", metavar="str", default=[],
                    help="filter images by wildcard pattern")
    rm.add_argument("-u", "--untagged", action="store_true",
                    help="remove images without tags", default=False)
    rm.add_argument("-f", "--force", action="store_true",
                    help="force image removal", default=False)
    rm.add_argument("-y", "--yes", action="store_true",
                    help="skip confirmation prompt for image removal", default=False)

    push = actions.add_parser("push", **desc("Push images"))
    push.add_argument("-p", "--pattern", action="append", metavar="str", default=[],
                      help="filter images by wildcard pattern")
    push.add_argument("-y", "--yes", action="store_true",
                      help="skip confirmation prompt for image push", default=False)

    pull = actions.add_parser("pull", **desc("Pull a image"))
    pull.add_argument("image", action="store", help="image name")
    pull.add_argument("-s", "--singularity", "--apptainer", action="store", metavar="path",
                      help="convert and store as sif image")
    pull.add_argument("-l", "--local", action="store_true",
                      help="use local repository to get image", default=False)
    pull.add_argument("--arch", action="store",
                      help="pull a image of different architecture")

    return _cmd
