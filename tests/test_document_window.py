"""Bench-free collection of the document window and session-document unit tests."""
from frappe_whatsapp.frappe_whatsapp.tests.test_document_window import (
	TestControllerBoundary,
	TestEvidence,
	TestExactWindow,
	TestIsOpen,
	TestLegacySendTemplateGuards,
	TestOutgoingDefault,
	TestPeerCandidates,
	TestSessionDocument,
)

__all__ = [
	"TestControllerBoundary",
	"TestEvidence",
	"TestExactWindow",
	"TestIsOpen",
	"TestLegacySendTemplateGuards",
	"TestOutgoingDefault",
	"TestPeerCandidates",
	"TestSessionDocument",
]
