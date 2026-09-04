"""Configuration (CS-OP-ARCH-002 §10).

Values may be `secret://NAME` references. This module NEVER resolves them --
it holds references, and `main.py` resolves the whole config once at startup
into a separate runtime copy. That separation is the point: this object may
be logged or republished, so it must never come to hold values.
"""

import os
from dataclasses import dataclass, field, fields
from typing import Any

SECRET_KEYS = ("oidc_client_secret",)

# Delivered material, never logged. The key is a credential; the cert is
# public but a 2 KB base64 blob in every boot line helps nobody.
MATERIAL_KEYS = ("tls_cert_b64", "tls_key_b64")


@dataclass
class Config:
    # runtime
    data_dir: str = "/data"
    tls: bool = True
    port: int = 0                     # 0 -> 8443 with TLS, 8080 without
    bind: str = "0.0.0.0"
    max_connections: int = 64
    read_timeout: int = 15

    # oidc
    oidc_client_id: str = ""
    # Defaults to the REFERENCE, never "". An empty default means a
    # directly-constructed Config silently carries a blank credential, and
    # the fail-loud boot check never fires because there is no reference to
    # fail to resolve.
    oidc_client_secret: str = "secret://OIDC_CLIENT_SECRET"
    oidc_redirect_uri: str = ""
    hosted_domain: str = "commsecurity.com.au"

    # tls material delivered through the environment (base64 PEM). Empty
    # means "use whatever is already at data/tls/". Both or neither.
    tls_cert_b64: str = ""
    tls_key_b64: str = ""

    # The person who gets every role on every entity at sign-in, ONLY while
    # no active admin exists anywhere (auth.sign_in). Empty disables it.
    bootstrap_admin_email: str = ""

    # backup
    backup_interval_s: int = 3600
    backup_keep: int = 48

    _sources: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------ paths
    @property
    def db_path(self):
        return os.path.join(self.data_dir, "ops.db")

    @property
    def backup_dir(self):
        return os.path.join(self.data_dir, "backups")

    @property
    def documents_dir(self):
        return os.path.join(self.data_dir, "documents")

    @property
    def secrets_path(self):
        return os.path.join(self.data_dir, "secrets", "store.json")

    @property
    def session_key_path(self):
        return os.path.join(self.data_dir, "secrets", "session.key")

    @property
    def tls_cert(self):
        return os.path.join(self.data_dir, "tls", "server.crt")

    @property
    def tls_key(self):
        return os.path.join(self.data_dir, "tls", "server.key")

    @property
    def effective_port(self):
        return self.port or (8443 if self.tls else 8080)

    # ----------------------------------------------------------- safety
    def secret_refs(self):
        """The subset main.py resolves. Kept explicit so a new secret has to
        be declared rather than inferred from a naming convention."""
        return {k: getattr(self, k) for k in SECRET_KEYS}

    def redacted(self) -> dict[str, Any]:
        """Safe to log. Secret-bearing fields are reported by KEY only --
        and if one somehow holds a value rather than a reference, this says
        so instead of printing it."""
        out = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            if f.name in SECRET_KEYS:
                out[f.name] = value if str(value).startswith("secret://") \
                    else "<value, not a reference>"
            elif f.name in MATERIAL_KEYS:
                out[f.name] = "<%d bytes>" % len(value) if value else ""
            else:
                out[f.name] = value
        return out

    def __str__(self):
        return f"Config({self.redacted()})"


def _bool(raw, default):
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "off", "false", "no", "")


def from_env(env=None):
    env = os.environ if env is None else env
    data_dir = env.get("OPS_DATA", "/data")
    cfg = Config(
        data_dir=data_dir,
        tls=_bool(env.get("OPS_TLS"), True),
        port=int(env.get("OPS_PORT", "0")),
        bind=env.get("OPS_BIND", "0.0.0.0"),
        max_connections=int(env.get("OPS_MAX_CONNECTIONS", "64")),
        read_timeout=int(env.get("OPS_READ_TIMEOUT", "15")),
        oidc_client_id=env.get("OIDC_CLIENT_ID", ""),
        oidc_client_secret=env.get("OIDC_CLIENT_SECRET",
                                   "secret://OIDC_CLIENT_SECRET"),
        oidc_redirect_uri=env.get("OIDC_REDIRECT_URI", ""),
        hosted_domain=env.get("OPS_HOSTED_DOMAIN", "commsecurity.com.au"),
        tls_cert_b64=env.get("OPS_TLS_CERT", "").strip(),
        tls_key_b64=env.get("OPS_TLS_KEY", "").strip(),
        bootstrap_admin_email=env.get("OPS_BOOTSTRAP_ADMIN", "").strip(),
        backup_interval_s=int(env.get("OPS_BACKUP_INTERVAL_S", "3600")),
        backup_keep=int(env.get("OPS_BACKUP_KEEP", "48")),
    )
    return cfg
