import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


YOUTUBE_PROVIDER = "youtube"
MAX_COOKIE_FILE_BYTES = 1024 * 1024
YOUTUBE_DOMAIN = "youtube.com"
MAX_COOKIE_EXPIRATION = 253402300799
AUTH_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-1PSIDCC",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
    "__Secure-3PSIDCC",
}


class CookieConfigurationError(Exception):
    """Raised when an imported cookie file is invalid or unsupported."""


class CookieEncryptionUnavailable(Exception):
    """Raised when cookie encryption is not configured for this deployment."""


class CookieDecryptionError(Exception):
    """Raised when stored cookie material cannot be decrypted or parsed."""


@dataclass(frozen=True)
class Cookie:
    domain: str
    include_subdomains: bool
    path: str
    secure: bool
    expires: int
    name: str
    value: str

    @property
    def is_auth_cookie(self) -> bool:
        return self.name in AUTH_COOKIE_NAMES

    @property
    def is_session_cookie(self) -> bool:
        return self.expires == 0


def _fernet() -> Fernet:
    key = settings.COOKIE_ENCRYPTION_KEY
    if not key:
        raise CookieEncryptionUnavailable(
            "YouTube cookie storage is disabled because "
            "COOKIE_ENCRYPTION_KEY is not configured."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CookieEncryptionUnavailable(
            "COOKIE_ENCRYPTION_KEY is invalid. Configure a valid Fernet key "
            "before importing provider cookies."
        ) from exc


def parse_netscape_cookies(data: bytes) -> list[Cookie]:
    if not data:
        raise CookieConfigurationError("The uploaded cookie file is empty.")
    if len(data) > MAX_COOKIE_FILE_BYTES:
        raise CookieConfigurationError(
            "The cookie file exceeds the 1 MiB upload limit."
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CookieConfigurationError(
            "The cookie file must be UTF-8 Netscape format."
        ) from exc

    cookies: list[Cookie] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line or raw_line.startswith("#"):
            if not raw_line.startswith("#HttpOnly_"):
                continue

        fields = raw_line.split("\t")
        if len(fields) != 7:
            raise CookieConfigurationError(
                f"Cookie file line {line_number} is not valid Netscape "
                "cookie format."
            )

        domain, include_subdomains, path, secure, expires, name, value = fields
        is_http_only = domain.startswith("#HttpOnly_")
        if is_http_only:
            domain = domain.removeprefix("#HttpOnly_")

        normalized_domain = domain.lower().lstrip(".").rstrip(".")
        if (
            normalized_domain != YOUTUBE_DOMAIN
            and not normalized_domain.endswith("." + YOUTUBE_DOMAIN)
        ):
            raise CookieConfigurationError(
                "Only cookies scoped to youtube.com are accepted. "
                "Export a YouTube-only cookie file."
            )
        if include_subdomains.upper() not in {"TRUE", "FALSE"}:
            raise CookieConfigurationError(
                f"Cookie file line {line_number} has an invalid domain flag."
            )
        if not path.startswith("/"):
            raise CookieConfigurationError(
                f"Cookie file line {line_number} has an invalid path."
            )
        if secure.upper() not in {"TRUE", "FALSE"}:
            raise CookieConfigurationError(
                f"Cookie file line {line_number} has an invalid secure flag."
            )
        if not name or any(character in name for character in "\r\n\t"):
            raise CookieConfigurationError(
                f"Cookie file line {line_number} has an invalid name."
            )
        if any(character in value for character in "\r\n\t"):
            raise CookieConfigurationError(
                f"Cookie file line {line_number} has an invalid value."
            )
        try:
            expiration = int(expires)
        except ValueError as exc:
            raise CookieConfigurationError(
                f"Cookie file line {line_number} has an invalid expiration."
            ) from exc
        if expiration < 0:
            raise CookieConfigurationError(
                f"Cookie file line {line_number} has an invalid expiration."
            )
        if expiration > MAX_COOKIE_EXPIRATION:
            raise CookieConfigurationError(
                f"Cookie file line {line_number} has an unsupported expiration."
            )

        stored_domain = (
            "#HttpOnly_" + domain if is_http_only else domain
        )
        cookies.append(
            Cookie(
                domain=stored_domain,
                include_subdomains=include_subdomains.upper() == "TRUE",
                path=path,
                secure=secure.upper() == "TRUE",
                expires=expiration,
                name=name,
                value=value,
            )
        )

    if not cookies:
        raise CookieConfigurationError(
            "The cookie file contains no YouTube cookies."
        )
    if not any(cookie.is_auth_cookie for cookie in cookies):
        raise CookieConfigurationError(
            "The cookie file does not contain a recognized YouTube "
            "authentication cookie. Export cookies after signing in."
        )
    return cookies


def encrypt_cookies(cookies: list[Cookie]) -> bytes:
    payload = json.dumps(
        [asdict(cookie) for cookie in cookies],
        separators=(",", ":"),
    ).encode("utf-8")
    return _fernet().encrypt(payload)


def decrypt_cookies(encrypted: bytes) -> list[Cookie]:
    try:
        payload = _fernet().decrypt(encrypted)
        records = json.loads(payload)
        cookies = [Cookie(**record) for record in records]
    except CookieEncryptionUnavailable:
        raise
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError,
            TypeError, ValueError, KeyError) as exc:
        raise CookieDecryptionError(
            "Stored YouTube credentials could not be decrypted. The "
            "administrator must verify COOKIE_ENCRYPTION_KEY."
        ) from exc
    if not cookies:
        raise CookieDecryptionError(
            "Stored YouTube credentials are empty. Import the cookie file "
            "again."
        )
    return cookies


def active_auth_cookies(
    cookies: list[Cookie],
    *,
    now: int | None = None,
) -> list[Cookie]:
    current_time = int(time.time()) if now is None else now
    return [
        cookie
        for cookie in cookies
        if cookie.is_auth_cookie
        and (cookie.is_session_cookie or cookie.expires > current_time)
    ]


def current_cookies(
    cookies: list[Cookie],
    *,
    now: int | None = None,
) -> list[Cookie]:
    current_time = int(time.time()) if now is None else now
    return [
        cookie
        for cookie in cookies
        if cookie.is_session_cookie or cookie.expires > current_time
    ]


def next_auth_expiration(
    cookies: list[Cookie],
) -> datetime | None:
    expirations = [
        cookie.expires
        for cookie in cookies
        if cookie.is_auth_cookie
        if not cookie.is_session_cookie
    ]
    return (
        datetime.fromtimestamp(min(expirations), timezone.utc)
        if expirations
        else None
    )


def serialize_netscape_cookies(cookies: list[Cookie]) -> bytes:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        lines.append(
            "\t".join(
                (
                    cookie.domain,
                    "TRUE" if cookie.include_subdomains else "FALSE",
                    cookie.path,
                    "TRUE" if cookie.secure else "FALSE",
                    str(cookie.expires),
                    cookie.name,
                    cookie.value,
                )
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def is_expired(cookies: list[Cookie], *, now: int | None = None) -> bool:
    return not active_auth_cookies(cookies, now=now)

