"""The smallest loud stand-in for Frappe that the bench-free suite needs.

`install()` registers it only when the real `frappe` cannot be imported, so on a
bench the real framework always wins. Pure helpers behave like Frappe v16
(`_dict`, `_`, the exception classes, `throw`, `generate_hash`, the date and
number utilities). Everything that needs a site — `frappe.db.*`, `get_doc`,
`log_error`, `get_installed_apps`, Document persistence, the integration
request helpers — raises `BenchRequired` unless the test patches that exact
call. A test that reaches the database by accident therefore fails instead of
passing against a fake.
"""

import datetime
import logging
import secrets
import sys
import types
import unittest


class BenchRequired(AssertionError):
    """A bench-free test reached something only a site can answer."""


def _tripwire(name):
    def call(*args, **kwargs):
        raise BenchRequired(f"frappe.{name} needs a bench: patch it in a bench-free test")

    call.__name__ = name.rsplit(".", 1)[-1]
    call.__qualname__ = call.__name__
    return call


class _dict(dict):
    """frappe._dict: keys as attributes, missing attributes are None."""

    __slots__ = ()
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__
    __setstate__ = dict.update

    def __getstate__(self):
        return self

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        return self

    def copy(self):
        return _dict(self)


class _LoudMapping(dict):
    """A site-derived mapping (e.g. EVENT_MAP): reading it needs a bench."""

    def __init__(self, name):
        super().__init__()
        self._name = name

    def _refuse(self, *args, **kwargs):
        raise BenchRequired(f"{self._name} needs a bench: patch it in a bench-free test")

    __contains__ = __getitem__ = get = keys = items = values = __iter__ = __len__ = _refuse


class _Database:
    """frappe.db: every method is a tripwire; tests patch the calls they expect."""

    def __init__(self):
        for method in (
            "after_commit", "commit", "count", "delete", "exists", "get_all", "get_single_value", "get_value",
            "get_values", "has_column", "release_savepoint", "rollback", "savepoint", "set_value", "sql",
            "table_exists",
        ):
            setattr(self, method, _tripwire("db." + method))


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    module.__file__ = __file__
    return module


def _build():
    class ValidationError(Exception):
        http_status_code = 417

    class PermissionError(Exception):  # noqa: A001 - mirrors frappe.PermissionError
        http_status_code = 403

    class DoesNotExistError(ValidationError):
        http_status_code = 404

    class UniqueValidationError(ValidationError):
        pass

    class DuplicateEntryError(NameError):
        pass

    def _(msg, lang=None, context=None):
        return msg

    def throw(msg, exc=ValidationError, title=None, is_minimizable=False, wide=False, as_list=False,
              primary_action=None):
        raise exc(msg)

    def msgprint(msg, *args, **kwargs):
        local.message_log.append(msg)

    def generate_hash(txt=None, length=56):
        return secrets.token_hex(length // 2 + 1)[:length]

    def whitelist(allow_guest=False, xss_safe=False, methods=None):
        def decorate(fn):
            whitelisted.append(fn)
            return fn

        return decorate

    def logger(module=None, *args, **kwargs):
        return logging.getLogger(module or "frappe")

    def get_datetime(value=None):
        if value is None:
            return datetime.datetime.now()
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, datetime.date):
            return datetime.datetime.combine(value, datetime.time())
        return datetime.datetime.fromisoformat(str(value))

    def cint(value, default=0):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def add_to_date(date, years=0, months=0, weeks=0, days=0, hours=0, minutes=0, seconds=0, as_string=False,
                    as_datetime=False):
        if years or months:
            raise BenchRequired("frappe.utils.add_to_date with months/years is not stubbed")
        value = get_datetime(date) + datetime.timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes,
                                                        seconds=seconds)
        return str(value) if as_string else value

    whitelisted = []
    flags = _dict()
    local = types.SimpleNamespace(flags=flags, form_dict=_dict(), message_log=[], site=None)

    utils = _module(
        "frappe.utils",
        add_to_date=add_to_date,
        cint=cint,
        convert_utc_to_system_timezone=_tripwire("utils.convert_utc_to_system_timezone"),
        datetime=datetime,
        escape_html=_tripwire("utils.escape_html"),
        get_datetime=get_datetime,
        get_url=_tripwire("utils.get_url"),
        now=lambda: str(datetime.datetime.now()),
        now_datetime=datetime.datetime.now,
        nowdate=lambda: str(datetime.date.today()),
    )
    password = _module(
        "frappe.utils.password",
        get_decrypted_password=_tripwire("utils.password.get_decrypted_password"),
        set_encrypted_password=_tripwire("utils.password.set_encrypted_password"),
    )
    safe_exec = _module(
        "frappe.utils.safe_exec",
        get_safe_globals=_tripwire("utils.safe_exec.get_safe_globals"),
        safe_exec=_tripwire("utils.safe_exec.safe_exec"),
    )
    utils.password, utils.safe_exec = password, safe_exec

    class Document:
        """frappe.model.document.Document without persistence."""

        def __init__(self, *args, **kwargs):
            values = dict(args[0]) if args and isinstance(args[0], dict) else {}
            values.update(kwargs)
            self.__dict__.update(values)
            self.flags = _dict()

        def get(self, key, default=None):
            return self.__dict__.get(key, default)

        def set(self, key, value):
            self.__dict__[key] = value

        def is_new(self):
            return bool(self.get("__islocal")) or not self.get("name")

    for method in ("db_insert", "db_update", "delete", "insert", "reload", "save", "submit"):
        setattr(Document, method, _tripwire("model.document.Document." + method))

    class IntegrationTestCase(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise BenchRequired(f"{cls.__name__} is an IntegrationTestCase: run it with bench run-tests")

    document = _module("frappe.model.document", Document=Document)
    naming = _module("frappe.model.naming", make_autoname=_tripwire("model.naming.make_autoname"))
    model = _module(
        "frappe.model",
        default_fields=("doctype", "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"),
        document=document,
        naming=naming,
        numeric_fieldtypes=("Currency", "Int", "Long Int", "Float", "Percent", "Check"),
    )
    integrations_utils = _module(
        "frappe.integrations.utils",
        make_post_request=_tripwire("integrations.utils.make_post_request"),
        make_request=_tripwire("integrations.utils.make_request"),
    )
    integrations = _module("frappe.integrations", utils=integrations_utils)
    server_script_utils = _module(
        "frappe.core.doctype.server_script.server_script_utils",
        EVENT_MAP=_LoudMapping("frappe.core.doctype.server_script.server_script_utils.EVENT_MAP"),
    )
    server_script = _module("frappe.core.doctype.server_script", server_script_utils=server_script_utils)
    core_doctype = _module("frappe.core.doctype", server_script=server_script)
    core = _module("frappe.core", doctype=core_doctype)
    form_utils = _module("frappe.desk.form.utils", get_pdf_link=_tripwire("desk.form.utils.get_pdf_link"))
    desk_form = _module("frappe.desk.form", utils=form_utils)
    desk = _module("frappe.desk", form=desk_form)
    tests_utils = _module("frappe.tests.utils", FrappeTestCase=IntegrationTestCase)
    tests = _module("frappe.tests", IntegrationTestCase=IntegrationTestCase, UnitTestCase=unittest.TestCase,
                    utils=tests_utils)

    frappe = _module(
        "frappe",
        BenchRequired=BenchRequired,
        DoesNotExistError=DoesNotExistError,
        DuplicateEntryError=DuplicateEntryError,
        PermissionError=PermissionError,
        UniqueValidationError=UniqueValidationError,
        ValidationError=ValidationError,
        _=_,
        _dict=_dict,
        bold=lambda text: f"<strong>{text}</strong>",
        core=core,
        db=_Database(),
        desk=desk,
        flags=flags,
        generate_hash=generate_hash,
        integrations=integrations,
        local=local,
        logger=logger,
        model=model,
        msgprint=msgprint,
        request=None,
        session=_dict(user="Administrator"),
        tests=tests,
        throw=throw,
        utils=utils,
        whitelist=whitelist,
        whitelisted=whitelisted,
        form_dict=local.form_dict,
    )
    frappe.__path__ = []  # a package, so `import frappe.utils` resolves through sys.modules
    for name in (
        "cache", "clear_document_cache", "delete_doc", "enqueue_doc", "get_all", "get_cached_doc", "get_doc",
        "get_installed_apps", "get_meta", "get_roles", "get_single", "get_site_path", "get_traceback", "has_permission",
        "log_error", "new_doc", "only_for", "publish_realtime", "safe_eval", "set_user",
    ):
        setattr(frappe, name, _tripwire(name))

    modules = {
        "frappe": frappe,
        "frappe.core": core,
        "frappe.core.doctype": core_doctype,
        "frappe.core.doctype.server_script": server_script,
        "frappe.core.doctype.server_script.server_script_utils": server_script_utils,
        "frappe.desk": desk,
        "frappe.desk.form": desk_form,
        "frappe.desk.form.utils": form_utils,
        "frappe.integrations": integrations,
        "frappe.integrations.utils": integrations_utils,
        "frappe.model": model,
        "frappe.model.document": document,
        "frappe.model.naming": naming,
        "frappe.tests": tests,
        "frappe.tests.utils": tests_utils,
        "frappe.utils": utils,
        "frappe.utils.password": password,
        "frappe.utils.safe_exec": safe_exec,
    }
    for module in modules.values():
        if module is not frappe:
            module.__path__ = []
    return modules


def install():
    """Register the stand-in unless a real Frappe is importable. Returns True when stubbed."""
    if "frappe" in sys.modules:
        return getattr(sys.modules["frappe"], "BenchRequired", None) is BenchRequired
    try:
        import frappe  # noqa: F401
    except ImportError:
        sys.modules.update(_build())
        return True
    return False
