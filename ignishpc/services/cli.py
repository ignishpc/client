from ignishpc.common.formatter import SmartFormatter, desc


def _cmd(args):
    from ignishpc.services.services import _run
    return _run(args)


def _create_service(services, *args, **kargs):
    parser = services.add_parser(*args, **kargs)
    actions = parser.add_subparsers(dest="action", title="Available Actions", metavar="<action>")
    actions.required = True

    return {
        "parser": parser,
        "actions": actions,
        "start": actions.add_parser("start", **desc("Start the service")),
        "stop": actions.add_parser("stop", **desc("Stop the service")),
        "resume": actions.add_parser("resume", **desc("Resume the service")),
        "destroy": actions.add_parser("destroy", **desc("Destroy the service")),
        "status": actions.add_parser("status", **desc("Status of the service"))
    }


def setup(subparsers):
    parser = subparsers.add_parser("services", **desc("Docker-Based Service Management"))

    services = parser.add_subparsers(dest="service", title="Available Services", metavar="<service>")
    services.required = True

    services.add_parser("status", description="Display service status")

    registry = _create_service(services, "registry", **desc("Service for managing Docker image registry"),
                               formatter_class=SmartFormatter,
                               epilog="""Examples:
                                     | $ ignishpc services registry start --https-self
                                     | $ ignishpc services registry destroy
                                     Note: /etc/ignis/registry is mounted to use certs (domain.crt, domain.key, secret).
                                     """)

    registry["garbage"] = registry["actions"].add_parser("garbage", description="Run registry garbage collection")
    registry["garbage"].add_argument("-m", "--delete-untagged", action="store_true", default=False,
                                     help="delete manifests that are not currently referenced via tag")

    registry["start"].add_argument("-b", "--bind", action="store", metavar="address",
                                   help="address that should be bound to for internal cluster communications, "
                                        "default all interfaces")
    registry["start"].add_argument("-p", "--port", action="store", metavar="int", type=int,
                                   help="server port, default 5000")
    registry["start"].add_argument("-e", "--env", action="append", metavar="key=value", default=[],
                                   help="configure a registry enviroment variable")
    registry["start"].add_argument("--path", dest="path", action="store", metavar="str",
                                   help="path to store the registry, default /var/lib/ignis/registry")
    registry["start"].add_argument("--https", action="store_true", default=False,
                                   help="run registry with HTTPS, default port 443")
    registry["start"].add_argument("-f", "--force", dest="force", action="store_true",
                                   help="destroy if exists")

    registry_ui = _create_service(services, "registry-ui", **desc("Web interface for docker registry service"))
    registry_ui["start"].add_argument("-p", "--port", action="store", metavar="int", type=int,
                                      help="server Port, default 3000")
    registry_ui["start"].add_argument("-u", dest="url", action="store", metavar="str",
                                      help="url of your docker registry, by default search http in port 5000")
    registry_ui["start"].add_argument("-e", "--env", action="append", metavar="key=value", default=[],
                                      help="configure a registry enviroment variable")
    registry_ui["start"].add_argument("-v", "--volume", action="append", metavar="path", default=[],
                                      help="make a path available in the container")
    registry_ui["start"].add_argument("-f", "--force", dest="force", action="store_true",
                                      help="destroy if exists")

    etcd = _create_service(services, "etcd", **desc("Service for managing etcd for container discovery"),
                           formatter_class=SmartFormatter,
                           epilog="""Examples:
                                         | $ ignishpc services etcd start --secure
                                         | $ ignishpc services etcd destroy
                                         Note: /etc/ignis/etcd is mounted to find certs.
                                         """)
    etcd["start"].add_argument("-b", "--bind", action="store", metavar="address",
                               help="address that should be bound to for internal cluster communications, "
                                    "default first interface")
    etcd["start"].add_argument("-p", "--port", action="store", metavar="int", type=int,
                               help="client port, default 2379")
    etcd["start"].add_argument("--extra-port", action="append", default=[],
                               help="extra ports")
    etcd["start"].add_argument("--path", dest="path", action="store", metavar="str",
                               help="path to store data, default /var/lib/ignis/etcd")
    etcd["start"].add_argument("-s", "--secure", action="store_true", default=False,
                               help="run etcd with self-signed transport")
    etcd["start"].add_argument("--extra-args", action="store", metavar="'args'",
                               help="binary extra arguments")
    etcd["start"].add_argument("-f", "--force", dest="force", action="store_true",
                               help="destroy if exists")

    # TODO

    return _cmd
