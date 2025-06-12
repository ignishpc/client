import os
import string
import random
import subprocess
import re
import sys

from ruamel.yaml import YAML, CommentedMap
from ruamel.yaml.parser import MarkedYAMLError
from ruamel.yaml.comments import CommentedBase

USER_CONFIG = os.getenv("IGNIS_USER_CONFIG", default=os.path.expanduser("~/.ignis/etc/ignis.yaml"))
SYSTEM_CONFIG = os.getenv("IGNIS_SYSTEM_CONFIG", default="/etc/ignis/ignis.yaml")
yaml = YAML()
_KEY_CRYPTO = "ignis.crypto.secret"
props = yaml.load("""
ignis:
  container:
    docker:
      registry: ""
      namespace: "ignishpc"
      default: "ignishpc"
      tag: "latest"
      root: false
      network: "default"
    singularity:
      source: "${HOME}/.ignis/images"
      default: "ignishpc.sif"
      network: "default"
    apptainer:
      source: "${HOME}/.ignis/images"
      default: "ignishpc.sif"
      network: "default"
    hostpipe: false
    writable: false
    #provider: ""
""")


def get_property(key, default=None):
    names = key.split(".")
    entry = props
    for name in names:
        if name not in entry or not isinstance(entry, CommentedMap):
            return default
        entry = entry[name]
    return entry


__true = re.compile("y|Y|yes|Yes|YES|true|True|TRUE|on|On|ON")


def get_bool(key, default=None):
    value = get_property(key, default)
    return value is not None and __true.match(str(value)) is not None


def get_string(key, default=None):
    return str(get_property(key, default))


def has_property(key):
    names = key.split(".")
    entry = props
    for name in names:
        if name not in entry or not isinstance(entry, CommentedMap):
            return False
        entry = entry[name]
    return True


def set_property(key, value):
    names = key.split(".")
    entry = props
    for name in names[:-1]:
        if name not in entry or not isinstance(entry, CommentedMap):
            entry[name] = CommentedMap()
        entry = entry[name]
    entry[names[-1]] = value


def format_image(name):
    if "/" not in name:
        namespace = get_string("ignis.container.docker.namespace", default="ignishpc")
        if len(namespace) > 0 and namespace[-1] != "/":
            namespace += "/"
        name = namespace + name

    if name.count("/") == 1:
        registry = get_string("ignis.container.docker.registry", default="")
        if len(registry) > 0 and registry[-1] != "/":
            registry += "/"
        name = registry + name

    if ":" not in name or name.rindex(":") > name.rindex("/"):
        tag = get_string("ignis.container.docker.tag", default="")
        if len(tag) > 0 and tag[0] != ':':
            tag = ":" + tag
        name = name + tag

    return name


def default_image():
    prefix = ""
    provider = get_string("ignis.container.provider")
    if get_string("ignis.container.provider") != "docker":
        source = get_string(f"ignis.container.{provider}.source")
        image = source + ("" if source.endswith("/") else "/") + get_string(f"ignis.container.{provider}.default")
        if os.path.exists(image) or ":" in image:
            return image
        prefix = "docker://"

    return prefix + format_image(get_string("ignis.container.docker.default"))


def network():
    provider = get_string("ignis.container.provider")
    return get_string(f"ignis.container.{provider}.network")


def yaml_merge(target, source):
    target.ca.items.update(source.ca.items)
    target.ca.comment = source.ca.comment or target.ca.comment
    target.ca.pre = source.ca.pre or target.ca.pre
    target.ca.end = source.ca.end or target.ca.end
    for key, value in source.items():
        if key in target and isinstance(value, CommentedMap) and isinstance(target[key], CommentedMap):
            yaml_merge(target[key], value)
        else:
            target[key] = value


def __check_openssl():
    try:
        return subprocess.run(["openssl", "version"], capture_output=True).returncode == 0
    except:
        return False


__env_vars = re.compile(r'(?<!\\)\$(\{([^}]+)\})')
__has_openssl = __check_openssl()


def __yaml_expand(m):
    if m.group(2) in os.environ:
        return os.environ[m.group(2)]
    return m.group(2)


def __yaml_encode(v):
    if not has_property(_KEY_CRYPTO):
        raise RuntimeError(f"'{_KEY_CRYPTO}' not found")
    if not __has_openssl:
        raise RuntimeError("openssl is not available")
    secret = get_string(_KEY_CRYPTO)
    cmd = "openssl aes-256-cbc -pbkdf2 -a -e -kfile"
    encoded = subprocess.run(cmd.split() + [secret], input=v, capture_output=True, encoding="utf-8", check=True).stdout
    return "$" + encoded.strip() + "$"


def __yaml_update(m):
    for key in m:
        value = m[key]
        if isinstance(value, CommentedMap):
            m[key] = __yaml_update(value)
        elif isinstance(value, str) and len(value) > 0:
            if "${" in value:
                m[key] = __env_vars.sub(__yaml_expand, value)
            elif not (value[0] == '$' and value[-1] == '$') and key[0] == '$' and key[-1] == '$':
                try:
                    m[key] = __yaml_encode(value)
                except Exception as ex:
                    print(f"warning: secret key '{key}' found but: {str(ex)}", file=sys.stderr)
    return m


def read_file_config(path, update=True):
    with open(path) as file:
        data = yaml.load(file.read())
        if data is None:
            return CommentedMap()
        return __yaml_update(data) if update else data


def _check_singularity():
    try:
        return subprocess.run(["singularity", "version"], capture_output=True).returncode == 0
    except:
        return False

def _check_apptainer():
    try:
        return subprocess.run(["apptainer", "version"], capture_output=True).returncode == 0
    except:
        return False


def load_config(path):
    ok = True
    for file in [SYSTEM_CONFIG, USER_CONFIG, path]:
        if file is not None and os.path.exists(file):
            try:
                yaml_merge(props, read_file_config(file, False))
            except:
                ok = False

    if not has_property("ignis.container.provider"):
        set_property("ignis.container.provider",
        "apptainer" if _check_apptainer() else ("singularity" if _check_singularity() else "docker"))

    if not has_property("ignis.wdir"):
        set_property("ignis.wdir", os.getcwd())

    if has_property(_KEY_CRYPTO):
        set_property(_KEY_CRYPTO, os.path.expandvars(get_string(_KEY_CRYPTO)))

    if not has_property("ignis.submitter.env.TZ") or \
            not has_property("ignis.driver.env.TZ") or \
            not has_property("ignis.executor.env.TZ"):
        if "TZ" in os.environ:
            tz = os.getenv("TZ")
        else:
            import datetime
            tz = datetime.datetime.now().astimezone().strftime('%Z')
        set_property("ignis.submitter.env.TZ", tz)
        set_property("ignis.driver.env.TZ", tz)
        set_property("ignis.executor.env.TZ", tz)

    if not has_property("ignis.submitter.binds./tmp"):
        set_property("ignis.submitter.binds./tmp","/tmp")

    __yaml_update(props)

    return ok or (path is not None and os.path.exists(path))


def random_password(k=10):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=k))
