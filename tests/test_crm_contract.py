"""Every call frappe_whatsapp makes into CRM binds to the pinned CRM source.

43b1b33 hands governed sends to `crm.api.outbox_bridge`, so this app must ship
with the CRM commit that defines it. A renamed function or a changed parameter
would otherwise surface as an ImportError or TypeError on a customer's send.
Every `from crm.api.<module> import <name>` in the app is found by AST, each
call to that name in the importing function is bound against the signature
the CRM source declares, and the two schema facts the bridge relies on are
read from the same source.

CRM_SOURCE must point at a checkout of the pinned CRM commit (CI: CRM_REF in
.github/workflows/release-ci.yml). A missing checkout fails; it never skips.
"""

import ast
import inspect
import json
import os
import pathlib
import unittest

APP = pathlib.Path(__file__).resolve().parents[1] / "frappe_whatsapp"

# Imports the bridge and the outbox depend on; the scan must keep finding them.
EXPECTED_IMPORTS = {
    ("crm.api.conversation_activity", "internal_apply_customer_activity"),
    ("crm.api.outbox", "require_dispatch"),
    ("crm.api.outbox_bridge", "governing_conversation"),
    ("crm.api.outbox_bridge", "queue_transcript"),
    ("crm.api.outbox_bridge", "requeue_transcript"),
    ("crm.api.outbox_delivery", "apply_delivery_receipt"),
    ("crm.api.outbox_legacy", "guard_legacy_send"),
}
# `from crm.api import conversations` hands the module to another function
# (coexistence._conversation_service); its one call is named here explicitly.
MODULE_IMPORTS = {("crm.api", "conversations")}
MODULE_CALLS = {("coexistence.py", "internal_apply_provider_event"): "crm.api.conversations"}


def crm_source():
    value = os.environ.get("CRM_SOURCE")
    if not value:
        raise AssertionError("CRM_SOURCE is not set: point it at the pinned CRM checkout")
    root = pathlib.Path(value)
    if not (root / "crm" / "api").is_dir():
        raise AssertionError(f"CRM_SOURCE={value} has no crm/api")
    return root


def crm_module(dotted):
    path = crm_source().joinpath(*dotted.split(".")).with_suffix(".py")
    if not path.is_file():
        raise AssertionError(f"{dotted} does not exist in the pinned CRM source")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def top_level(tree, name, kind):
    for node in tree.body:
        if isinstance(node, kind) and node.name == name:
            return node
    return None


def signature(function):
    """inspect.Signature for an AST function definition (defaults are placeholders)."""
    args, parameters = function.args, []
    positional = args.posonlyargs + args.args
    first_default = len(positional) - len(args.defaults)
    for index, arg in enumerate(positional):
        kind = inspect.Parameter.POSITIONAL_ONLY if index < len(args.posonlyargs) \
            else inspect.Parameter.POSITIONAL_OR_KEYWORD
        default = None if index >= first_default else inspect.Parameter.empty
        parameters.append(inspect.Parameter(arg.arg, kind, default=default))
    if args.vararg:
        parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
                                            default=inspect.Parameter.empty if default is None else None))
    if args.kwarg:
        parameters.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def app_sources():
    for path in sorted(APP.rglob("*.py")):
        if "tests" in path.relative_to(APP).parts or path.name.startswith("test_"):
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def crm_imports_and_calls():
    imports, modules, calls = set(), set(), []
    for path, tree in app_sources():
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = {}
            for node in ast.walk(function):
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "crm":
                    for alias in node.names:
                        if crm_source().joinpath(*node.module.split("."), alias.name).with_suffix(".py").is_file():
                            modules.add((node.module, alias.name))
                        else:
                            imports.add((node.module, alias.name))
                            local[alias.asname or alias.name] = (node.module, alias.name)
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in local:
                    calls.append((local[node.func.id], node, f"{path.relative_to(APP)}:{node.lineno}"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                module = MODULE_CALLS.get((path.name, node.func.attr))
                if module:
                    calls.append(((module, node.func.attr), node, f"{path.relative_to(APP)}:{node.lineno}"))
    return imports, modules, calls


def bind(function, call):
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
        raise AssertionError("a call into CRM uses *args/**kwargs; bind it explicitly in this test")
    signature(function).bind(*[object()] * len(call.args), **{kw.arg: object() for kw in call.keywords})


class TestCrmContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.imports, cls.modules, cls.calls = crm_imports_and_calls()

    def test_the_scan_still_sees_every_crm_entry_point(self):
        self.assertEqual(self.imports, EXPECTED_IMPORTS)
        self.assertEqual(self.modules, MODULE_IMPORTS)
        called = {target for target, _, _ in self.calls}
        self.assertEqual(called, EXPECTED_IMPORTS | {(module, attr) for (_, attr), module in MODULE_CALLS.items()})

    def test_every_call_binds_to_the_crm_signature(self):
        for (module, name), call, where in self.calls:
            with self.subTest(call=f"{module}.{name}", at=where):
                function = top_level(crm_module(module), name, (ast.FunctionDef, ast.AsyncFunctionDef))
                self.assertIsNotNone(function, f"{module}.{name} is not a top-level function in the pinned CRM")
                try:
                    bind(function, call)
                except TypeError as error:
                    self.fail(f"{where} calls {module}.{name} in a way CRM does not accept: {error}")

    def test_crm_refusals_are_marked_for_send_outgoing_to_reraise(self):
        refused = top_level(crm_module("crm.api.outbox_bridge"), "NativeSendRefused", ast.ClassDef)
        self.assertIsNotNone(refused, "crm.api.outbox_bridge.NativeSendRefused is gone")
        marked = [node for node in refused.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "native_refusal" for target in node.targets)]
        self.assertEqual(len(marked), 1)
        self.assertIs(ast.literal_eval(marked[0].value), True)

    def test_outbound_intent_has_the_transcript_link_bulk_retry_reads(self):
        path = crm_source() / "crm/fcrm/doctype/crm_outbound_intent/crm_outbound_intent.json"
        fields = {field["fieldname"]: field for field in json.loads(path.read_text(encoding="utf-8"))["fields"]}
        self.assertIn("transcript_message", fields)
        # A stored column holding the WhatsApp Message name (frappe.db.has_column + exists filter).
        self.assertIn(fields["transcript_message"]["fieldtype"], {"Data", "Link"})
        self.assertFalse(fields["transcript_message"].get("is_virtual"))
