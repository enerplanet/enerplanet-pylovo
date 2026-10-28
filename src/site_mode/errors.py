"""Errors raised by the site store, each carrying the HTTP status it maps to."""


class SiteError(Exception):
    """Base class; status_code is the HTTP status for the API response."""
    status_code = 400


class SiteNotFound(SiteError):
    """The site, or an object addressed inside it, does not exist."""
    status_code = 404


class SiteConflict(SiteError):
    """The request conflicts with the site's current state, such as a running job."""
    status_code = 409


class SiteValidationError(SiteError):
    """The request is well formed but violates a site rule."""
    status_code = 422
