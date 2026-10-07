"""
SchoolTenantMiddleware
======================
Reads the school slug from the URL path prefix  ``/s/<slug>/...``  and
attaches the matching ``School`` instance to ``request.school``.

Path convention
---------------
Every school-scoped URL looks like:

    /s/st-marys/dashboard/
    /s/lincoln-high/assignment/42/

The middleware strips the ``/s/<slug>`` segment so that the rest of the
URL resolver (``digitalexams/urls.py``) sees the familiar  ``/dashboard/``
paths unchanged.  This means **no existing URL names or ``reverse()`` calls
need to change** — only the prefix is new.

Exempt paths
------------
Some paths must work *without* a school slug:

* ``/admin/``                — Django admin (platform super-admins)
* ``/superadmin/``          — ExamHub super-admin dashboard (Step 6)
* ``/__debug__/``           — Django Debug Toolbar (dev only)
* Any path listed in the ``TENANT_EXEMPT_PATHS`` setting

If the request path does not start with ``/s/``, and is not on the exempt
list, the middleware returns a  404  so that bare school URLs are never
accidentally accessible without a tenant context.

request attributes set
----------------------
``request.school``
    The resolved ``School`` instance (always active — inactive schools get
    a  403).  ``None`` on exempt paths.

``request.path_info`` / ``request.path``
    Re-written to strip the  ``/s/<slug>``  prefix so the URL resolver and
    all ``reverse()`` calls work without modification.

``request.school_url_prefix``
    Set to  ``/s/<slug>``  on tenant requests (empty string on exempt paths)
    so templates can build absolute links like
    ``{{ request.school_url_prefix }}{% url 'dashboard' %}``.
"""

from django.conf import settings
from django.http import Http404
from django.core.exceptions import PermissionDenied


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_exempt_paths():
    """Return the tuple of path prefixes that bypass school resolution."""
    defaults = (
        '/admin/',
        '/superadmin/',
        '/__debug__/',
        '/static/',
        '/media/',
    )
    extra = getattr(settings, 'TENANT_EXEMPT_PATHS', ())
    return defaults + tuple(extra)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class SchoolTenantMiddleware:
    """
    Resolves ``/s/<slug>/...`` URLs to a ``School`` tenant and sets
    ``request.school``.  All other paths that are not on the exempt list
    return 404 so that no school-scoped content leaks through bare URLs.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # ----------------------------------------------------------------
        # 0. Handle bare root URL — redirect to login or school dashboard.
        # ----------------------------------------------------------------
        if request.path_info == '/':
            from django.shortcuts import redirect
            if request.user.is_authenticated:
                if request.user.school:
                    # Keep ?sebquit=1 (Safe Exam Browser's quit address after submitting) so the
                    # "you can close Safe Exam Browser" page still shows if SEB didn't quit itself.
                    quit_qs = '?sebquit=1' if request.GET.get('sebquit') == '1' else ''
                    return redirect(f'/s/{request.user.school.slug}/{quit_qs}')
                if request.user.is_superuser:
                    return redirect('/superadmin/')
                return redirect('/admin/')
            return redirect('login')

        # ----------------------------------------------------------------
        # 1. Check exempt paths first — no tenant resolution required.
        # ----------------------------------------------------------------
        exempt_paths = _get_exempt_paths()
        if any(request.path_info.startswith(p) for p in exempt_paths):
            request.school = None
            request.school_url_prefix = ''
            return self.get_response(request)

        # ----------------------------------------------------------------
        # 2. Require /s/<slug>/ prefix for all non-exempt paths.
        # ----------------------------------------------------------------
        if not request.path_info.startswith('/s/'):
            # If the user is authenticated and belongs to a school, auto-route bare paths
            # like /classes/ or /activity/ to their school prefix /s/<slug>/...
            if request.user.is_authenticated and request.user.school:
                from django.shortcuts import redirect
                qs = request.META.get('QUERY_STRING')
                target = f'/s/{request.user.school.slug}{request.path_info}'
                if qs:
                    target += f'?{qs}'
                return redirect(target)
            # A bare path like /dashboard/ with no school prefix is a 404.
            raise Http404("No school prefix found in URL.")

        # ----------------------------------------------------------------
        # 3. Parse the slug out of /s/<slug>/rest/of/path
        # ----------------------------------------------------------------
        # path_info looks like:  /s/st-marys/dashboard/
        #                         ^  ^^^^^^^  ^^^^^^^^^
        #                         |  slug     remainder
        #                         /s/ prefix (3 chars)
        parts = request.path_info[3:].split('/', 1)  # strip leading '/s/'
        slug = parts[0]
        remainder = '/' + parts[1] if len(parts) > 1 else '/'

        # ----------------------------------------------------------------
        # 4. Resolve the School (404 if unknown slug).
        # ----------------------------------------------------------------
        # Local import avoids circular import at module load time.
        from digitalexams.models import School

        try:
            school = School.objects.get(slug=slug)
        except School.DoesNotExist:
            raise Http404(f"No school found with slug '{slug}'.")

        # ----------------------------------------------------------------
        # 5. Block inactive schools with a clear 403.
        # ----------------------------------------------------------------
        if not school.is_active:
            raise PermissionDenied(
                f"The school '{school.name}' is currently inactive. "
                "Please contact your administrator."
            )

        # Verify authenticated users belong to this school (platform superusers are exempt).
        if request.user.is_authenticated and not request.user.is_superuser:
            if request.user.school_id and request.user.school_id != school.id:
                raise PermissionDenied("You do not have access to this school.")

        # ----------------------------------------------------------------
        # 6. Rewrite the path so the URL resolver sees the slug-free path.
        #    We save the original for logging / redirects if needed.
        # ----------------------------------------------------------------
        request.school = school
        request.school_url_prefix = f'/s/{slug}'
        request._school_original_path = request.path_info  # kept for debugging

        # Temporarily rewrite path so Django's URL resolver works normally.
        original_path = request.path_info
        request.path_info = remainder
        request.path = remainder

        response = self.get_response(request)

        # Restore the real path on the way out (some logging middleware reads it).
        request.path_info = original_path
        request.path = original_path

        return response
