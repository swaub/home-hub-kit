import os
import tomllib
import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
EXAMPLE_PATH = CONFIG_DIR / "config.example.toml"
DEFAULT_PATH = CONFIG_DIR / "config.toml"
ENV_VAR = "HOMEHUB_CONFIG"
EFFORTS = ("low", "medium", "high")


class ConfigError(ValueError):
    pass


@dataclass
class PathsConfig:
    data_dir: str = "~/.local/share/homehub"
    state_dir: str = "~/.local/state/homehub"


@dataclass
class BrainConfig:
    model: str = "claude-haiku-5-5"
    effort: str | None = "low"
    max_turns: int = 20
    context_minutes: float = 5.0


@dataclass
class HomeAssistantConfig:
    url: str = ""


@dataclass
class NotifyConfig:
    url: str = ""
    topic: str = ""


@dataclass
class BackupConfig:
    dir: str = "~/.local/share/homehub/backups"
    keep: int = 14
    copy_to: str = ""


@dataclass
class FeaturesConfig:
    google: bool = True
    kdeconnect: bool = False
    home_assistant: bool = False
    adb: bool = False
    network_scans: bool = True
    ps5: bool = False
    bluetooth: bool = False


@dataclass
class LightsConfig:
    devices: list[str] = field(default_factory=list)


@dataclass
class RokuConfig:
    devices: list[str] = field(default_factory=list)


@dataclass
class MediaConfig:
    folder: str = ""


@dataclass
class ProxmoxConfig:
    name: str = "Proxmox server"
    host: str = ""
    mac: str = ""


@dataclass
class DirectionsConfig:
    home: str = ""


@dataclass
class CastConfig:
    device: str = ""


@dataclass
class NewsConfig:
    local_feed: str = ""
    local_name: str = ""


@dataclass
class WebConfig:
    port: int = 0
    host: str = "127.0.0.1"


@dataclass
class RemoteConfig:
    port: int = 0
    host: str = "0.0.0.0"
    address: str = "10.77.0.1"
    peers: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    tls_port: int = 0
    tls_cert: str = ""
    tls_key: str = ""
    public_name: str = ""


@dataclass
class PackagesConfig:
    scan_minutes: int = 15


@dataclass
class MinecraftConfig:
    root: str = ""
    cpus: int = 6
    max_memory_gb: int = 16
    public_host: str = ""


@dataclass
class LoggingConfig:
    retention_days: int = 30


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    home_assistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    web: WebConfig = field(default_factory=WebConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    minecraft: MinecraftConfig = field(default_factory=MinecraftConfig)
    packages: PackagesConfig = field(default_factory=PackagesConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    cast: CastConfig = field(default_factory=CastConfig)
    directions: DirectionsConfig = field(default_factory=DirectionsConfig)
    lights: LightsConfig = field(default_factory=LightsConfig)
    roku: RokuConfig = field(default_factory=RokuConfig)
    proxmox: ProxmoxConfig = field(default_factory=ProxmoxConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    source: Path | None = field(default=None, metadata={"internal": True})

    def data_dir(self):
        return Path(self.paths.data_dir).expanduser()

    def state_dir(self):
        return Path(self.paths.state_dir).expanduser()




def _type_name(value):
    return {bool: "true/false", int: "a whole number", float: "a number", str: "text"}.get(type(value), type(value).__name__)


def _check(value, annotation, where):
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        if value is None:
            return None
        inner = [a for a in typing.get_args(annotation) if a is not type(None)][0]
        return _check(value, inner, where)
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{where} must be a list, not {_type_name(value)}")
        (item,) = typing.get_args(annotation)
        return [_check(v, item, f"{where}[{i}]") for i, v in enumerate(value)]
    if is_dataclass(annotation):
        return _build(annotation, value, where)
    if annotation is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false, not {_type_name(value)}")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be a whole number, not {_type_name(value)}")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} must be a number, not {_type_name(value)}")
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be text in quotes, not {_type_name(value)}")
        return value
    raise ConfigError(f"{where} has an unsupported type {annotation}")


def _build(cls, table, where):
    if not isinstance(table, dict):
        raise ConfigError(f"{where or 'the config'} must be a table of settings")
    known = {f.name: f for f in fields(cls) if not f.metadata.get("internal")}
    unknown = sorted(set(table) - set(known))
    if unknown:
        raise ConfigError(f"unknown setting {where + '.' if where else ''}{unknown[0]}")
    values = {}
    for name, f in known.items():
        if name in table:
            values[name] = _check(table[name], f.type, f"{where}.{name}" if where else name)
    return cls(**values)


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def validate(cfg):
    b, lg = cfg.brain, cfg.logging
    for name, value in vars(cfg.paths).items():
        _require(value.strip(), f"paths.{name} must not be empty")
    _require(b.context_minutes >= 0, "brain.context_minutes must be 0 (a fresh session every time) or more")
    _require(b.model.strip(), "brain.model must not be empty")
    _require(b.effort is None or b.effort in EFFORTS, f"brain.effort must be one of {', '.join(EFFORTS)}")
    _require(b.max_turns >= 1, "brain.max_turns must be at least 1")
    _require(lg.retention_days >= 1, "logging.retention_days must be at least 1")
    _require(0 <= cfg.web.port <= 65535, "web.port must be 0 (off) or a port number")
    _require(cfg.web.host.strip(), "web.host must not be empty")
    _require(0 <= cfg.remote.port <= 65535, "remote.port must be 0 (off) or a port number")
    _require(cfg.remote.address.strip(), "remote.address must be the hub's VPN address")
    _require(all(entry.count("=") == 1 and all(part.strip() for part in entry.split("=")) for entry in cfg.remote.peers),
             "remote.peers entries must look like \"10.77.0.2=person_id\"")
    _require(all(entry.partition("=")[0].strip() and entry.partition("=")[2].strip().startswith(("http://", "https://"))
                 for entry in cfg.remote.apps), "remote.apps entries must look like \"Cockpit=https://10.77.0.1:9090\"")
    _require(0 <= cfg.remote.tls_port <= 65535, "remote.tls_port must be 0 (off) or a port number")
    _require(not cfg.remote.tls_port or (cfg.remote.tls_cert and cfg.remote.tls_key and cfg.remote.public_name),
             "remote.tls_port needs tls_cert, tls_key and public_name")
    _require(0 <= cfg.packages.scan_minutes <= 1440, "packages.scan_minutes must be 0 (off) to 1440")
    _require(1 <= cfg.minecraft.cpus <= 64, "minecraft.cpus must be between 1 and 64")
    _require(1 <= cfg.minecraft.max_memory_gb <= 256, "minecraft.max_memory_gb must be between 1 and 256")
    _require(cfg.backup.dir.strip(), "backup.dir must not be empty")
    _require(cfg.backup.keep >= 1, "backup.keep must be at least 1")
    _require(bool(cfg.notify.url.strip()) == bool(cfg.notify.topic.strip()), "notify.url and notify.topic must be set together")
    _require(not cfg.features.home_assistant or cfg.home_assistant.url.strip(), "home_assistant.url is needed when features.home_assistant is on")
    return cfg


def parse_config(data, source=None):
    if not isinstance(data, dict):
        raise ConfigError("the config must be a table of settings")
    cfg = _build(Config, data, "")
    cfg.source = source
    return validate(cfg)


def config_path(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_PATH if DEFAULT_PATH.exists() else EXAMPLE_PATH


def load_config(path=None):
    path = config_path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse_config(data, source=path)
