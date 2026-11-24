"""Canonical identity shared by the D1 progress queue producers."""
import re
from urllib.parse import urlsplit

_URL = re.compile(r'^(https?)://([^/?#]*)([^#]*)(?:#.*)?$', re.IGNORECASE)


def progress_identity_key(value):
    """Ignore scheme/www/fragment, lowercase authority, preserve path and query spelling."""
    value = str(value).strip()
    match = _URL.fullmatch(value)
    if not match:
        raise ValueError(f'invalid progress URL: {value!r}')
    scheme, authority, path_query = match.groups()
    parsed = urlsplit(value)
    if not parsed.netloc or parsed.username is not None or parsed.password is not None or '@' in authority:
        raise ValueError(f'invalid progress URL: {value!r}')
    authority = authority.lower()
    if authority.startswith('www.'):
        authority = authority[4:]
    if (scheme.lower() == 'https' and authority.endswith(':443')) or (scheme.lower() == 'http' and authority.endswith(':80')):
        authority = authority[:authority.rfind(':')]
    if not path_query or path_query.startswith('?'):
        path_query = '/' + path_query
    return authority + path_query
