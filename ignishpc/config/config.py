import os.path
import sys
import shutil
import subprocess

from ignishpc.common import configuration


def _run(args):
    {
        "info": _info,
        "list": _list,
        "set": _set,
        "get": _get,
        "rm": _rm
    }[args.action](args)


def _check_config(path):
    try:
        configuration.read_file_config(path)
        return "OK"
    except FileNotFoundError:
        return "NOT FOUND"
    except configuration.MarkedYAMLError as ex:
        return ex.problem + " " + ex.problem_mark
    except Exception as ex:
        return str(ex).replace("\n", " ")


def _check_docker():
    import docker
    try:
        info = docker.from_env().version()
        if "Version" in info:
            return info["Version"]
        return "OK"
    except Exception as ex:
        return str(ex).replace("\n", " ")


def _check_singularity():
    try:
        info = subprocess.run(["singularity", "version"], capture_output=True, encoding="utf-8", check=True)
        return info.stdout.strip()
    except:
        return "NOT FOUND"

def _check_apptainer():
    try:
        info = subprocess.run(["apptainer", "version"], capture_output=True, encoding="utf-8", check=True)
        return info.stdout.strip()
    except:
        return "NOT FOUND"


def _info(args):
    print("Property files:")
    print(f"  System: ({configuration.SYSTEM_CONFIG}): {_check_config(configuration.SYSTEM_CONFIG)}")
    print(f"  user: ({configuration.USER_CONFIG}): {_check_config(configuration.USER_CONFIG)}")
    if args.config is not None:
        print(f"  Cli: ({args.config}): {_check_config(args.config)}")
    else:
        print("  Cli: NONE")

    print("Container providers:")
    print("  Docker:", _check_docker())
    print("  Singularity:", _check_singularity())
    print("  Apptainer:", _check_apptainer())

    print("Job Environment: ")
    print("  Working directory:", configuration.get_string("ignis.wdir"))
    print("  Container provider:", configuration.get_string("ignis.container.provider"))
    print("  Default image:", configuration.default_image())


def _list(args):
    if not args.split:
        configuration.yaml.dump(configuration.props, sys.stdout)
    else:
        configs = list()
        try:
            configs.append(configuration.read_file_config(configuration.SYSTEM_CONFIG))
        except:
            pass
        try:
            configs.append(configuration.read_file_config(configuration.USER_CONFIG))
        except:
            pass
        if args.config:
            try:
                configs.append(configuration.read_file_config(args.config))
            except:
                pass
        configuration.yaml.dump_all(configs, sys.stdout)


def _set(args):
    file = configuration.SYSTEM_CONFIG if args.system else configuration.USER_CONFIG

    if os.path.exists(file):
        config = configuration.read_file_config(file)
    else:
        config = configuration.CommentedMap()

    for prop in args.props:
        if "=" not in prop or len(prop.split('=')[0].strip()) == 0:
            raise RuntimeError(prop + " is not a valid property")
        key, value = prop.split('=', maxsplit=1)
        keys = key.split('.')
        entry = config
        for k in keys[:-1]:
            if k not in entry:
                entry[k] = configuration.CommentedMap()
            entry = entry[k]
        entry[keys[-1]] = value

    if not os.path.exists(os.path.dirname(file)):
        os.makedirs(os.path.dirname(file), mode=0o755)
    if os.path.exists(file):
        shutil.copy(file, file + ".bak")
    with open(file, "w") as file:
        configuration.yaml.dump(config, file)


def _get(args):
    try:
        if args.user:
            config = configuration.read_file_config(configuration.USER_CONFIG)
        elif args.system:
            config = configuration.read_file_config(configuration.SYSTEM_CONFIG)
        else:
            config = configuration.props
    except:
        config = configuration.CommentedMap()

    for key in args.keys:
        try:
            keys = key.split('.')
            entry = config
            for k in keys:
                if k not in entry:
                    raise RuntimeError("key '" + key + "' not found")
                entry = entry[k]
            if isinstance(entry, configuration.CommentedBase) and args.plain_value:
                raise RuntimeError("key '" + key + "' does not have a plain value")
            if args.only_value:
                print(str(entry))
            else:
                print(key + "=" + str(entry))
        except Exception as ex:
            if args.fail:
                raise ex
            elif args.only_value:
                print()


def _remove_key(config, key):
    stack = [config]
    keys = key.split('.')
    for k in keys[:-1]:
        if k not in stack[-1]:
            return False
        stack.append(stack[-1][k])
    if keys[-1] in stack[-1]:
        del stack[-1][keys[-1]]
        for i in range(len(stack) - 1, 1, -1):
            if len(stack[i]) == 0:
                stack[i - 1].popitem(stack[i])
            else:
                break
        return True
    else:
        return False


def _rm(args):
    not_removed = set(args.keys)

    if not args.system:
        try:
            config = configuration.read_file_config(configuration.USER_CONFIG)
            for key in list(not_removed):
                if _remove_key(config, key) and not args.all:
                    not_removed.remove(key)

            shutil.copy(configuration.USER_CONFIG, configuration.USER_CONFIG + ".bak")
            with open(configuration.USER_CONFIG, "w") as file:
                configuration.yaml.dump(config, file)
        except FileNotFoundError:
            pass

    if not args.user:
        try:
            config = configuration.read_file_config(configuration.SYSTEM_CONFIG)
            for key in args.keys:
                _remove_key(config, key)
            shutil.copy(configuration.SYSTEM_CONFIG, configuration.SYSTEM_CONFIG + ".bak")
            with open(configuration.SYSTEM_CONFIG, "w") as file:
                configuration.yaml.dump(config, file)
        except FileNotFoundError:
            pass
