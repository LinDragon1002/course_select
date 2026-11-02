from django.conf import settings

from courses.session_middleware import (
    ADMIN_SESSION_COOKIE_NAME,
    ADMIN_SESSION_COOKIE_PATH,
)


def force_admin_login(client, user, backend=None):
    """Authenticate a test client specifically for Admin requests."""
    client.force_login(user, backend=backend)
    session_cookie = client.cookies.pop(settings.SESSION_COOKIE_NAME)
    client.cookies[ADMIN_SESSION_COOKIE_NAME] = session_cookie.value
    admin_cookie = client.cookies[ADMIN_SESSION_COOKIE_NAME]
    admin_cookie["path"] = ADMIN_SESSION_COOKIE_PATH
    admin_cookie["httponly"] = settings.SESSION_COOKIE_HTTPONLY
    admin_cookie["secure"] = settings.SESSION_COOKIE_SECURE
    admin_cookie["samesite"] = settings.SESSION_COOKIE_SAMESITE
