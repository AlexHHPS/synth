"""Per-service routing and capacity, configured by the operator; never changes the shared gateway."""
import os
from urllib.parse import urlsplit

LOCAL_GATEWAYS = frozenset({"http://127.0.0.1:20128/v1", "http://host.docker.internal:20128/v1"})
DEFAULT_MODELS = "local-combo,instant-combo,cheap-combo"


def approved_endpoint(value, local=frozenset()):
    """Loopback/compose endpoints listed in `local`, or HTTPS `/v1` without credentials, query or fragment."""
    if value in local:
        return True
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and bool(parsed.hostname) and parsed.path == "/v1"
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment))


def configured_models():
    """Route names the operator has configured in their gateway (OMNIROUTE_MODELS, comma separated)."""
    names = {name.strip() for name in os.environ.get("OMNIROUTE_MODELS", DEFAULT_MODELS).split(",") if name.strip()}
    combo = os.environ.get("OMNIROUTE_COMBO", "").strip()
    if combo:
        names.add(combo)
    if not names or any(len(name) > 128 for name in names):
        raise ValueError("unapproved_combo")
    return frozenset(names)


MODELS = configured_models()


def gateway():
    value = os.environ.get("OMNIROUTE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
    if not approved_endpoint(value, LOCAL_GATEWAYS):
        raise ValueError("unapproved_gateway")
    return value


def capacity():
    value = int(os.environ.get("HERMES_CONCURRENCY", "2"))
    if not 1 <= value <= 8:
        raise ValueError("invalid_hermes_capacity")
    return value
